//+------------------------------------------------------------------+
//| TrendBotEA.mq5 - PHASE 2: backend connection + REAL trading      |
//| Sends signed heartbeats, receives and validates configuration,   |
//| acknowledges it, executes commands (PAUSE/RESUME/CLOSE/...),     |
//| runs the TrendBot scalp engine on CLOSED candles only, and       |
//| places real market orders via OrderSend after a final EA-side    |
//| safety gate. The EA is the final execution authority.            |
//| LIVE mode remains hard-blocked here until explicitly unlocked.   |
//+------------------------------------------------------------------+
#property strict
#property version     "2.000"
#property description "TrendBot EA - automated DEMO trading with backend control."

#include <TrendBot/TrendBotComm.mqh>
#include <TrendBot/TrendBotRuntime.mqh>
#include <TrendBot/TrendBotTrade.mqh>

input string InpBackendUrl       = "http://127.0.0.1:8000";   // must be added under Tools > Options > Expert Advisors > Allow WebRequest
input string InpSecretFile       = "TrendBot_secret.txt";     // in MQL5\Files; first line = your TB_EA_SECRET
input int    InpHeartbeatSeconds = 10;                        // used until the backend configuration arrives
input bool   InpReportAllSymbols = false;                     // false = only symbols in Market Watch
input int    InpMaxSymbols       = 300;
input int    InpDeviationPoints  = 20;                        // max slippage in points for market orders
input int    InpHistoryBars      = 400;                       // bars loaded per timeframe for the engine

string     g_secret = "";
TB_Runtime g_rt;
bool       g_have_cfg = false;
int        g_applied_version = 0;
string     g_applied_hash = "";
bool       g_paused = false;
ulong      g_last_hb_ms = 0;
ulong      g_last_ok_ms = 0;
ulong      g_last_sym_ms = 0;
ulong      g_last_flush_ms = 0;
string     g_status = "starting";
string     g_last_error = "";
string     g_ack_body = "";             // acknowledgement waiting to be delivered
int        g_rejected_version = 0;
ulong      g_rejected_ms = 0;
string     g_done_cmds[];
int        g_ndone = 0;

//--- trading state (candle-close gate + duplicate protection) -----------------
long       g_last_m5_open[8];           // last processed M5 bar open time per symbol slot
string     g_last_sym[8];               // symbol for each slot
int        g_nslots = 0;
string     g_used_setups[];             // setup keys already traded (persisted to file)
int        g_nused = 0;
datetime   g_day_start_equity = 0;      // equity snapshot at the daily reset boundary
double     g_day_start_equity_val = 0.0;
string     g_trade_state_file()
  {
   return "TrendBot_state_" + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) + ".txt";
  }

void SaveTradeState()
  {
   int h = FileOpen(g_trade_state_file(), FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE) return;
   FileWriteLine(h, IntegerToString((long)g_day_start_equity));
   FileWriteLine(h, DoubleToString(g_day_start_equity_val, 8));
   for(int i = 0; i < g_nused; i++)
      FileWriteLine(h, g_used_setups[i]);
   FileClose(h);
  }

void LoadTradeState()
  {
   int h = FileOpen(g_trade_state_file(), FILE_READ | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE) return;
   if(!FileIsEnding(h)) g_day_start_equity = (long)StringToInteger(FileReadLine(h));
   if(!FileIsEnding(h)) g_day_start_equity_val = StringToDouble(FileReadLine(h));
   while(!FileIsEnding(h))
     {
      string s = FileReadLine(h);
      if(s == "") continue;
      ArrayResize(g_used_setups, g_nused + 1);
      g_used_setups[g_nused++] = s;
     }
   FileClose(h);
   if(g_day_start_equity_val > 0.0)
      g_day_start_equity = (long)g_rt.reset_utc_offset_seconds * 0 + g_day_start_equity; // keep stored boundary
  }

bool SetupUsed(const string key)
  {
   for(int i = 0; i < g_nused; i++)
      if(g_used_setups[i] == key) return true;
   return false;
  }

void MarkSetupUsed(const string key)
  {
   if(SetupUsed(key)) return;
   ArrayResize(g_used_setups, g_nused + 1);
   g_used_setups[g_nused++] = key;
   if(g_nused > 4000)                       // prune oldest: keep the most recent 2000
     {
      int keep = 2000;
      for(int i = 0; i < keep; i++)
         g_used_setups[i] = g_used_setups[g_nused - keep + i];
      g_nused = keep;
      ArrayResize(g_used_setups, keep);
     }
   SaveTradeState();
  }

//--- reliable new-bar detection on CLOSED candles only. Returns the most recently
//--- CLOSED M5 bar's open time (index 1 - the forming bar at index 0 is never used).
//--- Bar identity = symbol + timeframe + bar open time: NewClosedBar() below guarantees
//--- each closed bar is evaluated EXACTLY ONCE, immune to multiple ticks. After an EA
//--- restart the first sighting only re-syncs the baseline (no blind trade), and the
//--- persisted SetupUsed() memory blocks any duplicate order for an already-handled setup.
long ClosedBarTimeframe(const string sym, const ENUM_TIMEFRAMES tf)
  {
   datetime t1 = iTime(sym, tf, 1);        // index 1 = the most recently CLOSED bar
   if(t1 == 0) return 0;
   return (long)t1;
  }

bool NewClosedBar(const int slot, const string sym, const long open_t)
  {
   if(g_last_sym[slot] != sym)
     {
      g_last_sym[slot] = sym;
      g_last_m5_open[slot] = open_t;       // first sighting: adopt as baseline, do not trade it blind
      return false;
     }
   if(open_t == g_last_m5_open[slot]) return false;   // same candle: many ticks arrive here, evaluate ONCE
   g_last_m5_open[slot] = open_t;
   return true;
  }

int SlotFor(const string sym)
  {
   for(int i = 0; i < g_nslots; i++)
      if(g_last_sym[i] == sym) return i;
   if(g_nslots >= 8) return -1;
   g_last_sym[g_nslots] = sym;
   g_last_m5_open[g_nslots] = 0;
   return g_nslots++;
  }

//--- load CLOSED bars only (shift starts at 1) into an MqlRates array, newest last
bool LoadClosedBars(const string sym, const ENUM_TIMEFRAMES tf, const int want, MqlRates &rates[])
  {
   int have = Bars(sym, tf);
   int n = MathMin(want, have - 1);          // exclude the forming bar entirely
   if(n < 50) return false;
   ArrayResize(rates, n);
   for(int i = 0; i < n; i++)
     {
      // copy_rates_range is the reliable way to fetch historical closed bars
      MqlRates tmp[1];
      if(CopyRates(sym, tf, i + 1, 1, tmp) != 1) return false;
      rates[n - 1 - i] = tmp[0];              // oldest first, engine expects chronological order
     }
   return true;
  }

void RunSymbol(const string sym)
  {
   if(!g_have_cfg || !ConfigFresh()) return;
   if(g_paused || g_rt.kill_switch) return;
   if(g_rt.core.mode == "SIGNAL_ONLY") return;   // gate still evaluates but no orders are ever sent
   int slot = SlotFor(sym);
   if(slot < 0) return;
   long m5closed = ClosedBarTimeframe(sym, PERIOD_M5);
   if(m5closed == 0) return;
   if(!NewClosedBar(slot, sym, m5closed)) return; // the just-closed M5 bar was already evaluated (or is a baseline)

   TB_Engine d1, h4, m15, m5;
   TB_Params ep;
   TB_DefaultParams(ep);
   ep.point = SymbolInfoDouble(sym, SYMBOL_POINT);
   MqlRates rd[], rh[], r15[], r5[];
   if(!LoadClosedBars(sym, PERIOD_D1, InpHistoryBars / 4, rd) ||
      !LoadClosedBars(sym, PERIOD_H4, InpHistoryBars, rh) ||
      !LoadClosedBars(sym, PERIOD_M15, InpHistoryBars, r15) ||
      !LoadClosedBars(sym, PERIOD_M5, InpHistoryBars, r5))
     { TB_QueueEvent("ENGINE_NOTE", "\"symbol\":\"" + TB_Esc(sym) + "\",\"note\":\"insufficient_history\""); return; }
   d1.Run(rd, ArraySize(rd), ep);
   h4.Run(rh, ArraySize(rh), ep);
   m15.Run(r15, ArraySize(r15), ep);
   m5.Run(r5, ArraySize(r5), ep);

   TB_Scalp sc;
   TB_ScalpParams sp = g_rt.scalp;
   TB_SymbolSpec bspec;
   if(TB_ReadSpec(sym, bspec)) sp.spread = bspec.spread;
   sc.Run(d1, h4, m15, m5, sp);

   // find the CANDIDATE emitted for the just-closed bar and act on it exactly once
   TB_BrokerAcct acct;
   TB_ReadBroker(acct);
   for(int i = 0; i < sc.m_nsig; i++)
     {
      if(sc.m_sig[i].stype != "CANDIDATE") continue;
      if(sc.m_sig[i].time != m5closed) continue;         // only the JUST-CLOSED bar triggers entry
      string key = sym + "|M5|" + IntegerToString(m5closed) + "|" + sc.m_sig[i].reason + "|" +
                   DoubleToString(sc.m_sig[i].price, 8) + "|" + DoubleToString(sc.m_sig[i].price2, 8);
      bool rejected = false;
      string why = "";
      if(SetupUsed(key))
        { rejected = true; why = "duplicate_setup"; }    // covers restart/reconnect/heartbeat retries
      else
        {
         double entry = TB_QuotePrice(sym, sc.m_sig[i].dir);
         if(entry <= 0.0) { rejected = true; why = "no_quote"; }
         else
           {
            string chk = TB_TradeAllowed(g_rt, acct, bspec, ConfigFresh(), g_paused,
                                         sc.m_sig[i].dir, entry, sc.m_sig[i].price, sc.m_sig[i].price3);
            if(chk != "") { rejected = true; why = chk; }
            else
              {
               // position sizing identical to Python sizing.py: budget -> lots, rounded DOWN
               double risk_pct = g_rt.core.risk_percent;
               double budget = acct.equity * risk_pct / 100.0;
               // remaining-drawdown clamp mirrors drawdown.py next_trade_risk
               if(g_rt.core.max_open_risk_percent > 0)
                 {
                  double maxopen = acct.equity * g_rt.core.max_open_risk_percent / 100.0;
                  double used = OpenRiskMoney();
                  budget = MathMin(budget, MathMax(0.0, maxopen - used));
                 }
               if(budget <= 0.0) { rejected = true; why = "remaining_drawdown_or_open_risk_exhausted"; }
               else
                 {
                  double loss_per_lot = TB_LossPerLot(bspec, MathAbs(entry - sc.m_sig[i].price2));
                  if(loss_per_lot <= 0.0) { rejected = true; why = "loss_per_lot_zero"; }
                  else
                    {
                     double lots = TB_NormalizeLots(bspec, budget / loss_per_lot);
                     if(lots <= 0.0) { rejected = true; why = "min_lot_exceeds_risk_budget"; }
                     else if(!TB_MarginOk(sym, sc.m_sig[i].dir, lots, entry, acct.free_margin))
                        { rejected = true; why = "insufficient_margin"; }
                     else
                       {
                        // basket target: TP = entry + dir * target_multiplier * stop distance (shared budget, NOT per-position full risk)
                        double tp = sc.m_sig[i].price3;
                        string comment = "TB|" + IntegerToString(m5closed) + "|" + sc.m_sig[i].reason;
                        double fill = 0.0;
                        string rsn = "";
                        long ticket = TB_SendMarket(sym, sc.m_sig[i].dir, lots, sc.m_sig[i].price2, tp,
                                                    comment, InpDeviationPoints, g_rt.magic, bspec.digits, fill, rsn);
                        if(ticket <= 0) { rejected = true; why = rsn; }
                        else
                          {
                           MarkSetupUsed(key);
                           string ev = "\"symbol\":\"" + TB_Esc(sym) + "\",\"ticket\":" + IntegerToString(ticket) +
                                       ",\"dir\":" + IntegerToString(sc.m_sig[i].dir) + ",\"lots\":" + DoubleToString(lots, 8) +
                                       ",\"entry\":" + DoubleToString(fill, bspec.digits) +
                                       ",\"sl\":" + DoubleToString(sc.m_sig[i].price2, bspec.digits) +
                                       ",\"tp\":" + DoubleToString(tp, bspec.digits) +
                                       ",\"risk_money\":" + DoubleToString(TB_PositionRiskMoney(bspec, fill, sc.m_sig[i].price2, lots), 2) +
                                       ",\"setup\":\"" + TB_Esc(sc.m_sig[i].reason) + "\",\"bar\":" + IntegerToString(m5closed);
                           TB_QueueEvent("ORDER_FILLED", ev);
                           Print("TrendBot: ORDER ", (sc.m_sig[i].dir == 1 ? "BUY " : "SELL "), sym, " ", lots,
                                 " @ ", fill, " ticket ", ticket);
                          }
                       }
                  }
              }
         }
        }
      if(rejected)
        {
         MarkSetupUsed(key);   // a rejected setup must never be retried on a later tick/bar either
         TB_QueueEvent("TRADE_BLOCKED", "\"symbol\":\"" + TB_Esc(sym) + "\",\"reason\":\"" + TB_Esc(why) +
                      "\",\"bar\":" + IntegerToString(m5closed) + ",\"setup_kind\":\"" + TB_Esc(sc.m_sig[i].reason) + "\"");
         Print("TrendBot: TRADE BLOCKED ", sym, " reason=", why);
        }
     }
  }

//--- floating risk of OUR open positions (magic-filtered): mirrors executor.open_risk_total
double OpenRiskMoney()
  {
   double total = 0.0;
   for(int p = PositionsTotal() - 1; p >= 0; p--)
     {
      ulong tk = PositionGetTicket(p);
      if(tk == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_rt.magic) continue;
      string sym = PositionGetString(POSITION_SYMBOL);
      TB_SymbolSpec s;
      if(!TB_ReadSpec(sym, s)) continue;
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl    = PositionGetDouble(POSITION_SL);
      double lots  = PositionGetDouble(POSITION_VOLUME);
      if(sl <= 0.0) continue;
      total += TB_PositionRiskMoney(s, entry, sl, lots);
     }
   return total;
  }

//--- basket management on live positions: break-even move when configured trigger reached
void ManageOpenPositions()
  {
   if(!g_have_cfg) return;
   for(int p = PositionsTotal() - 1; p >= 0; p--)
     {
      ulong tk = PositionGetTicket(p);
      if(tk == 0) continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_rt.magic) continue;
      if(g_rt.be_method == "off" || g_rt.be_trigger_r <= 0.0) continue;   // "A"/"B"/"C" are the enabled methods
      string sym = PositionGetString(POSITION_SYMBOL);
      double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl    = PositionGetDouble(POSITION_SL);
      double cur   = PositionGetDouble(POSITION_PRICE_CURRENT);
      long   ptype = PositionGetInteger(POSITION_TYPE);
      int    dir   = (ptype == POSITION_TYPE_BUY) ? 1 : -1;
      double risk  = MathAbs(entry - sl);
      if(risk <= 0.0) continue;
      double progress = ((dir == 1) ? (cur - entry) : (entry - cur)) / risk;
      if(progress >= g_rt.be_trigger_r)
        {
         TB_SymbolSpec s;
         if(!TB_ReadSpec(sym, s)) continue;
         double be = entry + dir * (double)g_rt.be_offset_points * s.point;
         bool already_be = (dir == 1) ? (sl >= entry) : (sl <= entry);
         if(!already_be)
           {
            string rsn = "";
            if(TB_ModifySL((long)tk, g_rt.magic, be, s.digits, rsn))
               TB_QueueEvent("BE_APPLIED", "\"symbol\":\"" + TB_Esc(sym) + "\",\"ticket\":" + IntegerToString((long)tk) +
                             ",\"new_sl\":" + DoubleToString(be, s.digits));
           }
        }
     }
  }

//--- the configuration is only trusted while the backend keeps confirming it
bool ConfigFresh()
  {
   if(!g_have_cfg || g_last_ok_ms == 0) return false;
   return (GetTickCount64() - g_last_ok_ms) <= (ulong)g_rt.stale_config_seconds * 1000;
  }

string Num(const double v, const int d = 2) { return DoubleToString(v, d); }

string HeartbeatBody()
  {
   bool isDemo = (AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO);
   string b = "{\"applied_version\":" + IntegerToString(g_applied_version);
   b += ",\"mode\":\"" + (g_have_cfg ? g_rt.core.mode : "SIGNAL_ONLY") + "\"";
   b += ",\"account\":{\"login\":" + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) +
        ",\"balance\":" + Num(AccountInfoDouble(ACCOUNT_BALANCE)) + ",\"equity\":" + Num(AccountInfoDouble(ACCOUNT_EQUITY)) +
        ",\"free_margin\":" + Num(AccountInfoDouble(ACCOUNT_MARGIN_FREE)) + ",\"is_demo\":" + (isDemo ? "true" : "false") +
        ",\"currency\":\"" + TB_Esc(AccountInfoString(ACCOUNT_CURRENCY)) + "\"}";
   b += ",\"state\":{\"paused\":" + (g_paused ? "true" : "false") + ",\"locked_out\":false,\"orders_enabled\":" +
        ((g_have_cfg && !g_paused && !g_rt.kill_switch && ConfigFresh() && g_rt.core.mode != "SIGNAL_ONLY") ? "true" : "false") +
        ",\"config_fresh\":" + (ConfigFresh() ? "true" : "false") + "}";
   // report OUR open positions so the backend/dashboard can show live broker state
   string ps = "[";
   bool firstp = true;
   for(int p = PositionsTotal() - 1; p >= 0; p--)
     {
      ulong tk = PositionGetTicket(p);
      if(tk == 0) continue;
      if(g_rt.magic != 0 && PositionGetInteger(POSITION_MAGIC) != g_rt.magic) continue;
      if(!firstp) ps += ",";
      firstp = false;
      ps += "{\"ticket\":" + IntegerToString((long)tk) +
            ",\"symbol\":\"" + TB_Esc(PositionGetString(POSITION_SYMBOL)) + "\"" +
            ",\"dir\":" + (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "1" : "-1") +
            ",\"lots\":" + Num(PositionGetDouble(POSITION_VOLUME), 2) +
            ",\"entry\":" + Num(PositionGetDouble(POSITION_PRICE_OPEN), 5) +
            ",\"current\":" + Num(PositionGetDouble(POSITION_PRICE_CURRENT), 5) +
            ",\"sl\":" + Num(PositionGetDouble(POSITION_SL), 5) +
            ",\"tp\":" + Num(PositionGetDouble(POSITION_TP), 5) +
            ",\"pl\":" + Num(PositionGetDouble(POSITION_PROFIT), 2) +
            ",\"open_time\":" + IntegerToString((long)PositionGetInteger(POSITION_TIME)) +
            ",\"magic\":" + IntegerToString(PositionGetInteger(POSITION_MAGIC)) +
            ",\"comment\":\"" + TB_Esc(PositionGetString(POSITION_COMMENT)) + "\"}";
     }
   b += ",\"positions\":" + ps + "]";
   b += ",\"gate\":\"ea_v2\"";
   ulong now = GetTickCount64();
   if(g_last_sym_ms == 0 || now - g_last_sym_ms > 600000)      // symbols on the first beat, then every 10 minutes
     {
      b += ",\"symbols\":" + TB_SymbolsJson(InpReportAllSymbols, InpMaxSymbols);
      g_last_sym_ms = now;
     }
   return b + "}";
  }

bool SendAck(const int version, const string hash, const bool ok, const string error)
  {
   string body = "{\"version\":" + IntegerToString(version) + ",\"hash\":\"" + TB_Esc(hash) + "\",\"ok\":" +
                 (ok ? "true" : "false") + ",\"error\":\"" + TB_Esc(error) + "\"}";
   string resp, err;
   int code = TB_Post(InpBackendUrl, "/ea/config/ack", body, g_secret, resp, err);
   if(code == 200) { g_ack_body = ""; return true; }
   g_ack_body = body;                   // retry on the next timer tick
   return false;
  }

//--- the configuration inside a heartbeat response: validate it independently, apply it, acknowledge it
void HandleConfig(TB_Json &j, const string rawResponse)
  {
   TB_Runtime cand;
   string err = "";
   bool ok = TB_RuntimeFromJson(j, "config.", cand, err) && TB_RuntimeValidate(cand, j, "config.", err);
   int ver = (int)TB_JN(j, "config.", "config_version");
   string hash = TB_JS(j, "config.", "config_hash");
   if(ok)
     {
      g_rt = cand;
      g_have_cfg = true;
      g_applied_version = cand.config_version;
      g_applied_hash = cand.config_hash;
      TB_SaveConfigRaw(rawResponse);
      Print("TrendBot: configuration version ", ver, " applied");
      SendAck(ver, hash, true, "");
      return;
     }
   Print("TrendBot: configuration version ", ver, " REJECTED: ", err);
   ulong now = GetTickCount64();
   if(ver != g_rejected_version || now - g_rejected_ms > 300000)       // report a refusal once per 5 minutes at most
     {
      g_rejected_version = ver;
      g_rejected_ms = now;
      SendAck(ver, hash, false, err);
     }
  }

bool CommandDone(const string id)
  {
   for(int i = 0; i < g_ndone; i++)
      if(g_done_cmds[i] == id) return true;
   return false;
  }

void HandleCommand(const string id, const string kind)
  {
   if(CommandDone(id)) return;                                   // never execute a command twice
   bool ok = false;
   string detail = "";
   if(kind == "PAUSE")
     { g_paused = true; ok = true; detail = "new entries blocked"; }
   else if(kind == "RESUME")
     {
      if(!ConfigFresh())                 detail = "refused: configuration is not fresh/verified";
      else if(g_rt.kill_switch)          detail = "refused: kill switch is on";
      else if(!TB_AccountAllowed(g_rt, (long)AccountInfoInteger(ACCOUNT_LOGIN))) detail = "refused: account is not in allowed_logins";
      else { g_paused = false; ok = true; detail = "resumed (safety checks passed)"; }
     }
   else if(kind == "CLOSE_POSITION" || kind == "POSITION_ACTION")
     {
      // dashboard close commands arrive as POSITION_ACTION with an embedded ticket; tolerate both shapes
      long tk = CommandTicket(id);
      if(tk <= 0) detail = "refused: no valid ticket in command";
      else
        {
         string rsn = "";
         if(TB_CloseTicket(tk, g_rt.magic, rsn)) { ok = true; detail = "closed ticket " + IntegerToString(tk); }
         else detail = "close failed: " + rsn;
        }
     }
   else if(kind == "CLOSE_BASKET" || kind == "CLOSE_ALL")
     {
      int n = 0;
      string fails = "";
      for(int p = PositionsTotal() - 1; p >= 0; p--)
        {
         ulong tki = PositionGetTicket(p);
         if(tki == 0) continue;
         if(PositionGetInteger(POSITION_MAGIC) != g_rt.magic) continue;
         string rsn = "";
         if(TB_CloseTicket((long)tki, g_rt.magic, rsn)) n++;
         else fails += IntegerToString((long)tki) + ":" + rsn + ";";
        }
      ok = (n > 0 && fails == "");
      detail = "closed " + IntegerToString(n) + " positions" + (fails != "" ? (" failures[" + fails + "]") : "");
     }
   else if(kind == "SHUTDOWN")
     { ok = true; detail = "EA removing itself"; }
   else
      detail = "unknown command";
   if(ArraySize(g_done_cmds) < g_ndone + 1) ArrayResize(g_done_cmds, g_ndone + 16);
   g_done_cmds[g_ndone++] = id;
   TB_QueueEvent("COMMAND_RESULT", "\"command_id\":\"" + TB_Esc(id) + "\",\"kind\":\"" + TB_Esc(kind) + "\",\"ok\":" +
                 (ok ? "true" : "false") + ",\"detail\":\"" + TB_Esc(detail) + "\"");
   if(kind == "SHUTDOWN" && ok)
     {
      string e;
      TB_FlushEvents(InpBackendUrl, g_secret, e);
      ExpertRemove();
     }
  }

//--- small side table: command_id -> embedded ticket (dashboard close commands)
string g_cmd_ids[64];
long   g_cmd_tix[64];
int    g_ncmds = 0;

void RememberCommandTicket(const string id, const long tk)
  {
   for(int i = 0; i < g_ncmds; i++)
      if(g_cmd_ids[i] == id) { g_cmd_tix[i] = tk; return; }
   if(g_ncmds >= 64) return;
   g_cmd_ids[g_ncmds] = id;
   g_cmd_tix[g_ncmds] = tk;
   g_ncmds++;
  }

long CommandTicket(const string id)
  {
   for(int i = 0; i < g_ncmds; i++)
      if(g_cmd_ids[i] == id) return g_cmd_tix[i];
   return 0;
  }

void Heartbeat()
  {
   string resp, err;
   int code = TB_Post(InpBackendUrl, "/ea/heartbeat", HeartbeatBody(), g_secret, resp, err);
   if(code != 200)
     {
      g_status = "backend unreachable";
      g_last_error = (code == -1) ? err : ("HTTP " + IntegerToString(code) + " " + StringSubstr(resp, 0, 80));
      return;
     }
   g_last_ok_ms = GetTickCount64();
   g_status = "connected";
   g_last_error = "";
   TB_Json j;
   if(!j.Parse(resp)) { g_status = "bad response"; g_last_error = j.err; return; }
   if(j.Path("config") >= 0)
      HandleConfig(j, resp);
   int cmds = j.Path("commands");
   for(int i = 0; i < j.Count(cmds); i++)
     {
      int c = j.At(cmds, i);
      // remember the ticket carried by POSITION_ACTION commands so HandleCommand can act on it
      string cid = j.Str(j.Member(c, "command_id"));
      int tkt = j.Member(c, "ticket");
      long tk = (tkt >= 0) ? (long)j.Num(tkt) : 0;
      RememberCommandTicket(cid, tk);
      HandleCommand(cid, j.Str(j.Member(c, "kind")));
     }
  }

void Panel()
  {
   string cfg = g_have_cfg ? ("v" + IntegerToString(g_applied_version) + " (" + g_rt.core.mode + ")") : "none yet";
   Comment("TrendBot EA - backend connection phase (no trading code)\n",
           "Backend: ", g_status, g_last_error != "" ? " - " + g_last_error : "", "\n",
           "Configuration applied: ", cfg, "   fresh: ", ConfigFresh() ? "yes" : "NO", "\n",
           "Paused: ", g_paused ? "yes" : "no", "   queued events: ", g_tb_nevents, "\n",
           "Account ", (long)AccountInfoInteger(ACCOUNT_LOGIN), g_have_cfg && TB_AccountAllowed(g_rt, (long)AccountInfoInteger(ACCOUNT_LOGIN)) ? " (allowed)" : " (NOT in allowed_logins)");
  }

int OnInit()
  {
   MathSrand((int)(GetTickCount() ^ (uint)TimeLocal()));
   int h = FileOpen(InpSecretFile, FILE_READ | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE)
     {
      Print("TrendBot: cannot read ", InpSecretFile, " in MQL5\\Files (put your TB_EA_SECRET on the first line).");
      return INIT_FAILED;
     }
   g_secret = FileReadString(h);
   FileClose(h);
   StringTrimLeft(g_secret);
   StringTrimRight(g_secret);
   if(StringLen(g_secret) < 24)
     {
      Print("TrendBot: the secret in ", InpSecretFile, " is shorter than 24 characters.");
      return INIT_FAILED;
     }
   if(StringFind(InpBackendUrl, "http") != 0)
     {
      Print("TrendBot: InpBackendUrl must start with http:// or https://");
      return INIT_FAILED;
     }
   // reload the last applied configuration; it stays UNVERIFIED (not fresh) until the backend confirms it
   string raw = TB_LoadConfigRaw();
   if(raw != "")
     {
      TB_Json j;
      TB_Runtime saved;
      string err;
      if(j.Parse(raw) && TB_RuntimeFromJson(j, "config.", saved, err) && TB_RuntimeValidate(saved, j, "config.", err))
        {
         g_rt = saved;
         g_have_cfg = true;
         g_applied_version = saved.config_version;
         g_applied_hash = saved.config_hash;
        }
     }
   LoadTradeState();                            // duplicate-setup memory survives EA restarts
   EventSetTimer(1);
   TB_QueueEvent("EA_STARTED", "\"version\":\"2.000\",\"trading_enabled\":true");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   Comment("");
  }

void OnTimer()
  {
   ulong now = GetTickCount64();
   int hb = g_have_cfg ? g_rt.heartbeat_seconds : InpHeartbeatSeconds;
   if(g_ack_body != "" && now - g_last_flush_ms > 2000)
     {
      string resp, err;
      if(TB_Post(InpBackendUrl, "/ea/config/ack", g_ack_body, g_secret, resp, err) == 200) g_ack_body = "";
     }
   if(now - g_last_hb_ms >= (ulong)hb * 1000)
     {
      g_last_hb_ms = now;
      Heartbeat();
     }
   if(g_tb_nevents > 0 && now - g_last_flush_ms >= 2000)
     {
      g_last_flush_ms = now;
      string err;
      TB_FlushEvents(InpBackendUrl, g_secret, err);
     }
   Panel();
  }

void OnTick()
  {
   // candle-close execution: evaluate the JUST-CLOSED M5 bar exactly once per symbol, then manage positions
   if(!g_have_cfg || g_paused || g_rt.kill_switch) return;
   if(g_rt.core.mode == "SIGNAL_ONLY") return;
   string syms[];
   int n = 0;
   for(int i = 0; i < SymbolsTotal(true); i++)
     {
      string nm = SymbolName(i, true);            // Market Watch symbols only
      if(nm == "") continue;
      ArrayResize(syms, n + 1);
      syms[n++] = nm;
      if(n >= 8) break;                            // matches the 8 evaluation slots
     }
   for(int i = 0; i < n; i++)
      RunSymbol(syms[i]);
   ManageOpenPositions();
  }

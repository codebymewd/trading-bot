//+------------------------------------------------------------------+
//| TrendBotEA.mq5 - PHASE 1: backend connection (NO TRADING CODE)   |
//| Sends signed heartbeats, receives and validates configuration,   |
//| acknowledges it, executes PAUSE/RESUME/SHUTDOWN, uploads events. |
//| There is no OrderSend/CTrade anywhere in this EA version.        |
//| Not yet compiled in MetaEditor.                                  |
//+------------------------------------------------------------------+
#property strict
#property version     "1.000"
#property description "TrendBot EA - backend connection phase. Cannot place orders."

#include <TrendBot/TrendBotComm.mqh>
#include <TrendBot/TrendBotRuntime.mqh>

input string InpBackendUrl       = "http://127.0.0.1:8000";   // must be added under Tools > Options > Expert Advisors > Allow WebRequest
input string InpSecretFile       = "TrendBot_secret.txt";     // in MQL5\Files; first line = your TB_EA_SECRET
input int    InpHeartbeatSeconds = 10;                        // used until the backend configuration arrives
input bool   InpReportAllSymbols = false;                     // false = only symbols in Market Watch
input int    InpMaxSymbols       = 300;

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
   b += ",\"state\":{\"paused\":" + (g_paused ? "true" : "false") + ",\"locked_out\":false,\"orders_enabled\":false" +
        ",\"config_fresh\":" + (ConfigFresh() ? "true" : "false") + "}";
   b += ",\"positions\":[]";
   b += ",\"gate\":\"n/a\"";
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
   else if(kind == "CLOSE_POSITION")
      detail = "not available: this EA version has no trading functions";
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
      HandleCommand(j.Str(j.Member(c, "command_id")), j.Str(j.Member(c, "kind")));
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
   EventSetTimer(1);
   TB_QueueEvent("EA_STARTED", "\"version\":\"1.000\"");
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
   // PHASE 1: nothing to do. Signal detection and (later) order logic arrive in later phases.
  }

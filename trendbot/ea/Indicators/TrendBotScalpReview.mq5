//+------------------------------------------------------------------+
//| TrendBotScalpReview.mq5 - SIGNAL-ONLY review of the scalp rules  |
//| D1+H4 gate -> M15 zones -> M5 trigger -> structure checks ->     |
//| indicator confirmation. Draws zones, candidates and rejections.  |
//| NO ORDERS are ever sent. Closed bars only. Not yet compiled.     |
//+------------------------------------------------------------------+
#property strict
#property indicator_chart_window
#property indicator_buffers 0
#property indicator_plots   0
#property version "0.1"

#include <TrendBot/TrendBotScalp.mqh>

input int    InpD1Bars        = 300;
input int    InpH4Bars        = 1000;
input int    InpM15Bars       = 3000;
input int    InpM5Bars        = 9000;
input bool   InpH4Only        = false;   // false = H4_AND_D1 gate
input bool   InpFvgSetup      = true;
input bool   InpSweepSetup    = true;
input bool   InpLinkedFvgOnly = true;
input int    InpTriggerWindow = 12;      // M5 bars after the zone is touched
input int    InpSweepMaxAge   = 12;      // M15 bars
input int    InpZoneMaxAge    = 24;      // M15 bars
input bool   InpPremDiscount  = true;
input double InpMaxRangePct   = 50.0;
input double InpSlBufferAtr   = 0.1;
input double InpMinSlAtr      = 0.3;
input double InpMaxSlAtr      = 3.0;
input double InpMaxChaseR     = 0.5;
input double InpTargetMult    = 3.0;
input double InpSpreadRatio   = 0.15;
input double InpSpread        = 0.0;     // price units (0 = ignore spread in this review)
input bool   InpUseRsi        = true;
input bool   InpUseMacd       = true;
input bool   InpUseBb         = true;
input bool   InpUseStoch      = true;
input bool   InpUseCci        = false;
input int    InpMinConfirm    = 3;
input int    InpMaxDraw       = 120;     // newest signals drawn
input bool   InpExportCsv     = true;

TB_Engine g_d1, g_h4, g_m15, g_m5;
TB_Scalp  g_scalp;
TB_ScalpParams g_p;
datetime  g_last = 0;
bool      g_ready = false;
int       g_obj = 0;

string NextName()
  {
   g_obj++;
   return "TBS_" + IntegerToString(g_obj);
  }

void Dot(const datetime t, const double price, const int code, const color clr)
  {
   string nm = NextName();
   if(ObjectCreate(0, nm, OBJ_ARROW, 0, t, price))
     {
      ObjectSetInteger(0, nm, OBJPROP_ARROWCODE, code);
      ObjectSetInteger(0, nm, OBJPROP_COLOR, clr);
      ObjectSetInteger(0, nm, OBJPROP_SELECTABLE, false);
     }
  }

void Seg(const datetime t1, const double p1, const datetime t2, const double p2, const color clr, const ENUM_LINE_STYLE style)
  {
   string nm = NextName();
   if(ObjectCreate(0, nm, OBJ_TREND, 0, t1, p1, t2, p2))
     {
      ObjectSetInteger(0, nm, OBJPROP_COLOR, clr);
      ObjectSetInteger(0, nm, OBJPROP_STYLE, style);
      ObjectSetInteger(0, nm, OBJPROP_RAY_RIGHT, false);
      ObjectSetInteger(0, nm, OBJPROP_SELECTABLE, false);
     }
  }

void Txt(const datetime t, const double price, const string text, const color clr)
  {
   string nm = NextName();
   if(ObjectCreate(0, nm, OBJ_TEXT, 0, t, price))
     {
      ObjectSetString(0, nm, OBJPROP_TEXT, text);
      ObjectSetInteger(0, nm, OBJPROP_COLOR, clr);
      ObjectSetInteger(0, nm, OBJPROP_FONTSIZE, 8);
      ObjectSetInteger(0, nm, OBJPROP_SELECTABLE, false);
     }
  }

void Box(const datetime t1, const double p1, const datetime t2, const double p2, const color clr)
  {
   string nm = NextName();
   if(ObjectCreate(0, nm, OBJ_RECTANGLE, 0, t1, p1, t2, p2))
     {
      ObjectSetInteger(0, nm, OBJPROP_COLOR, clr);
      ObjectSetInteger(0, nm, OBJPROP_FILL, true);
      ObjectSetInteger(0, nm, OBJPROP_BACK, true);
      ObjectSetInteger(0, nm, OBJPROP_SELECTABLE, false);
     }
  }

string TrendName(const int t)
  {
   if(t == TB_BULL) return "BULL";
   if(t == TB_BEAR) return "BEAR";
   return "RANGE";
  }

void BuildParams()
  {
   TB_DefaultScalpParams(g_p);
   g_p.gate_h4_only = InpH4Only;
   g_p.fvg_setup = InpFvgSetup;
   g_p.sweep_setup = InpSweepSetup;
   g_p.require_bos_linked_fvg = InpLinkedFvgOnly;
   g_p.trigger_window_bars = InpTriggerWindow;
   g_p.sweep_setup_max_age_bars = InpSweepMaxAge;
   g_p.zone_max_age_bars = InpZoneMaxAge;
   g_p.use_premium_discount = InpPremDiscount;
   g_p.max_entry_range_pct = InpMaxRangePct;
   g_p.sl_buffer_atr = InpSlBufferAtr;
   g_p.min_sl_atr = InpMinSlAtr;
   g_p.max_sl_atr = InpMaxSlAtr;
   g_p.max_chase_r = InpMaxChaseR;
   g_p.target_multiplier = InpTargetMult;
   g_p.max_spread_to_stop_ratio = InpSpreadRatio;
   g_p.spread = InpSpread;
   g_p.enabled[0] = InpUseRsi;
   g_p.enabled[1] = InpUseMacd;
   g_p.enabled[2] = InpUseBb;
   g_p.enabled[3] = InpUseStoch;
   g_p.enabled[4] = InpUseCci;
   g_p.minimum_confirmations = InpMinConfirm;
  }

bool InputsValid(string &why)
  {
   int nen = 0;
   for(int i = 0; i < 5; i++) if(g_p.enabled[i]) nen++;
   if(InpMinConfirm < 0 || InpMinConfirm > nen) { why = "minimum confirmations must be 0.." + IntegerToString(nen); return false; }
   if(!InpFvgSetup && !InpSweepSetup) { why = "enable at least one setup type"; return false; }
   if(InpTargetMult < 0.5 || InpTargetMult > 10.0) { why = "target multiplier must be 0.5..10"; return false; }
   if(InpMaxSlAtr <= InpMinSlAtr) { why = "max stop must exceed min stop"; return false; }
   if(InpTriggerWindow < 1 || InpSweepMaxAge < 1 || InpZoneMaxAge < 1) { why = "windows must be >= 1"; return false; }
   return true;
  }

bool LoadEngine(TB_Engine &e, const ENUM_TIMEFRAMES tf, const int bars)
  {
   MqlRates r[];
   ArraySetAsSeries(r, false);
   int got = CopyRates(_Symbol, tf, 1, bars, r);   // closed bars only
   if(got < 60) return false;
   TB_Params ep;
   TB_DefaultParams(ep);
   ep.point = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
   e.Run(r, got, ep);
   return true;
  }

string Px(const double v) { return DoubleToString(v, _Digits); }

void ExportBarsFile(TB_Engine &e, const ENUM_TIMEFRAMES tf)
  {
   int h = FileOpen("TrendBot_" + _Symbol + "_" + EnumToString(tf) + "_bars.csv", FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(h == INVALID_HANDLE) return;
   FileWrite(h, "#digits=" + IntegerToString(_Digits));
   FileWrite(h, "time", "open", "high", "low", "close");
   for(int i = 0; i < e.m_n; i++)
      FileWrite(h, (string)e.m_tm[i], Px(e.m_o[i]), Px(e.m_h[i]), Px(e.m_l[i]), Px(e.m_c[i]));
   FileClose(h);
  }

void ExportCsv()
  {
   ExportBarsFile(g_d1, PERIOD_D1);
   ExportBarsFile(g_h4, PERIOD_H4);
   ExportBarsFile(g_m15, PERIOD_M15);
   ExportBarsFile(g_m5, PERIOD_M5);
   int h = FileOpen("TrendBot_" + _Symbol + "_scalp_signals.csv", FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(h == INVALID_HANDLE) { Print("TrendBot: cannot write signals csv err=", GetLastError()); return; }
   string en = "";
   string nm[5] = {"RSI", "MACD", "BB", "STOCH", "CCI"};
   for(int i = 0; i < 5; i++)
      if(g_p.enabled[i]) { if(en != "") en += "|"; en += nm[i]; }
   FileWrite(h, "#digits=" + IntegerToString(_Digits));
   FileWrite(h, "#params",
             "gate_h4_only=" + IntegerToString((int)g_p.gate_h4_only),
             "fvg_setup=" + IntegerToString((int)g_p.fvg_setup),
             "sweep_setup=" + IntegerToString((int)g_p.sweep_setup),
             "require_bos_linked_fvg=" + IntegerToString((int)g_p.require_bos_linked_fvg),
             "trigger_window_bars=" + IntegerToString(g_p.trigger_window_bars),
             "sweep_setup_max_age_bars=" + IntegerToString(g_p.sweep_setup_max_age_bars),
             "zone_max_age_bars=" + IntegerToString(g_p.zone_max_age_bars),
             "use_premium_discount=" + IntegerToString((int)g_p.use_premium_discount),
             "max_entry_range_pct=" + DoubleToString(g_p.max_entry_range_pct, 8),
             "sl_buffer_atr=" + DoubleToString(g_p.sl_buffer_atr, 8),
             "min_sl_atr=" + DoubleToString(g_p.min_sl_atr, 8),
             "max_sl_atr=" + DoubleToString(g_p.max_sl_atr, 8),
             "max_chase_r=" + DoubleToString(g_p.max_chase_r, 8),
             "target_multiplier=" + DoubleToString(g_p.target_multiplier, 8),
             "max_spread_to_stop_ratio=" + DoubleToString(g_p.max_spread_to_stop_ratio, 8),
             "spread=" + DoubleToString(g_p.spread, 10),
             "enabled=" + en,
             "minimum_confirmations=" + IntegerToString(g_p.minimum_confirmations));
   FileWrite(h, "time", "type", "dir", "price", "price2", "price3", "reason", "detail");
   for(int i = 0; i < g_scalp.m_nsig; i++)
      FileWrite(h, (string)g_scalp.m_sig[i].time, g_scalp.m_sig[i].stype, IntegerToString(g_scalp.m_sig[i].dir),
                Px(g_scalp.m_sig[i].price), Px(g_scalp.m_sig[i].price2), Px(g_scalp.m_sig[i].price3),
                g_scalp.m_sig[i].reason, g_scalp.m_sig[i].detail);
   FileClose(h);
  }

void DrawAll()
  {
   int sec = PeriodSeconds(PERIOD_M5);
   int n = g_m5.m_n;
   if(n < 2) return;
   datetime lastBar = (datetime)g_m5.m_tm[n - 1];
   int from = MathMax(0, g_scalp.m_nsig - InpMaxDraw);
   for(int i = from; i < g_scalp.m_nsig; i++)
     {
      datetime t = (datetime)g_scalp.m_sig[i].time;
      string tp = g_scalp.m_sig[i].stype;
      int d = g_scalp.m_sig[i].dir;
      if(tp == "SETUP")
        {
         datetime en = (datetime)(lastBar + 6 * sec);
         for(int j = i + 1; j < g_scalp.m_nsig; j++)
            if(g_scalp.m_sig[j].stype == "SETUP_END" && g_scalp.m_sig[j].dir == d &&
               g_scalp.m_sig[j].price == g_scalp.m_sig[i].price && g_scalp.m_sig[j].price2 == g_scalp.m_sig[i].price2)
              { en = (datetime)(g_scalp.m_sig[j].time + sec); break; }
         Box(t, g_scalp.m_sig[i].price, en, g_scalp.m_sig[i].price2, (d == 1) ? clrDarkGreen : clrMaroon);
        }
      else if(tp == "CANDIDATE")
        {
         double e = g_scalp.m_sig[i].price, sl = g_scalp.m_sig[i].price2, tg = g_scalp.m_sig[i].price3;
         datetime end = (datetime)(t + 12 * sec);
         Dot(t, e, (d == 1) ? 233 : 234, (d == 1) ? clrLime : clrRed);
         Seg(t, e, end, e, clrWhite, STYLE_SOLID);
         Seg(t, sl, end, sl, clrTomato, STYLE_DASH);
         Seg(t, tg, end, tg, clrLimeGreen, STYLE_DASH);
         Txt(end, e, (d == 1 ? "BUY " : "SELL ") + g_scalp.m_sig[i].reason + " [" + g_scalp.m_sig[i].detail + "]", clrWhite);
        }
      else if(tp == "REJECT")
        {
         Dot(t, g_scalp.m_sig[i].price, 251, clrGray);
         Txt(t, g_scalp.m_sig[i].price, g_scalp.m_sig[i].reason, clrGray);
        }
     }
  }

void DrawPanel()
  {
   int n = g_m5.m_n;
   int d1 = (g_d1.m_n > 0) ? g_d1.m_trend_at[g_d1.m_n - 1] : 0;
   int h4 = (g_h4.m_n > 0) ? g_h4.m_trend_at[g_h4.m_n - 1] : 0;
   int gate = TB_Gate(d1, h4, InpH4Only);
   int cands = 0, rejs = 0, setups = 0;
   for(int i = 0; i < g_scalp.m_nsig; i++)
     {
      if(g_scalp.m_sig[i].stype == "CANDIDATE") cands++;
      if(g_scalp.m_sig[i].stype == "REJECT") rejs++;
      if(g_scalp.m_sig[i].stype == "SETUP") setups++;
     }
   string last = "none";
   for(int i = g_scalp.m_nsig - 1; i >= 0; i--)
      if(g_scalp.m_sig[i].stype == "CANDIDATE" || g_scalp.m_sig[i].stype == "REJECT")
        {
         last = g_scalp.m_sig[i].stype + " " + g_scalp.m_sig[i].reason + " " +
                TimeToString((datetime)g_scalp.m_sig[i].time, TIME_DATE | TIME_MINUTES);
         break;
        }
   Comment("TrendBot scalp review  (SIGNAL-ONLY, no orders)\n",
           "D1: ", TrendName(d1), "   H4: ", TrendName(h4), "   Gate: ", TrendName(gate), "\n",
           "M5 bars: ", n, "   Setups: ", setups, "   Candidates: ", cands, "   Rejects: ", rejs, "\n",
           "Last: ", last);
  }

bool Rebuild()
  {
   BuildParams();
   string why;
   if(!InputsValid(why))
     {
      Comment("TrendBot scalp review: invalid inputs - ", why);
      return false;
     }
   ObjectsDeleteAll(0, "TBS_");
   g_obj = 0;
   if(!LoadEngine(g_m5, PERIOD_M5, InpM5Bars) || !LoadEngine(g_m15, PERIOD_M15, InpM15Bars) ||
      !LoadEngine(g_h4, PERIOD_H4, InpH4Bars) || !LoadEngine(g_d1, PERIOD_D1, InpD1Bars))
     {
      Comment("TrendBot scalp review: waiting for D1/H4/M15/M5 history...");
      return false;
     }
   g_scalp.Run(g_d1, g_h4, g_m15, g_m5, g_p);
   DrawAll();
   DrawPanel();
   if(InpExportCsv)
      ExportCsv();
   ChartRedraw(0);
   return true;
  }

int OnInit()
  {
   IndicatorSetString(INDICATOR_SHORTNAME, "TrendBot scalp review");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   ObjectsDeleteAll(0, "TBS_");
   Comment("");
  }

int OnCalculate(const int rates_total, const int prev_calculated, const datetime &time[],
                const double &open[], const double &high[], const double &low[], const double &close[],
                const long &tick_volume[], const long &volume[], const int &spread[])
  {
   datetime cur = iTime(_Symbol, PERIOD_M5, 0);
   if(g_ready && cur == g_last)
      return rates_total;                 // recompute only when a new M5 bar opens
   g_ready = Rebuild();
   if(g_ready)
      g_last = cur;
   return rates_total;
  }

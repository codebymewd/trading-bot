//+------------------------------------------------------------------+
//| TrendBotStageA.mq5 - SIGNAL-ONLY visual detector (no trading)    |
//| Draws: swings, BOS/MSS, displacement, FVGs, liquidity levels,    |
//| sweeps; shows D1/H4/H1/exec trend and the H4+D1 gate; exports    |
//| bars + events CSV for the parity check (tools/compare_events.py).|
//| Uses CLOSED bars only (bar 1 and older) so nothing repaints.     |
//| Not yet compiled in MetaEditor.                                  |
//+------------------------------------------------------------------+
#property strict
#property indicator_chart_window
#property indicator_buffers 0
#property indicator_plots   0
#property version "0.1"

#include <TrendBot/TrendBotEngine.mqh>

enum TB_MitMode
  {
   TB_MIT_TOUCH = 1,   // touch
   TB_MIT_HALF  = 2,   // half
   TB_MIT_FULL  = 3    // full
  };

input int        InpExecBars     = 3000;   // chart-TF bars analysed
input int        InpD1Bars       = 400;
input int        InpH4Bars       = 1500;
input int        InpH1Bars       = 3000;
input bool       InpH4Only       = false;  // false = H4_AND_D1 gate
input int        InpMaxDraw      = 250;    // newest events drawn
input bool       InpDrawLevels   = true;
input bool       InpExportCsv    = true;
input int        InpSwingLeft    = 3;
input int        InpSwingRight   = 3;
input double     InpMinSwingAtr  = 0.5;
input double     InpBosBufferAtr = 0.05;
input double     InpDispBodyAtr  = 1.2;
input double     InpDispRatio    = 0.6;
input int        InpDispMaxBars  = 3;
input double     InpEqTolAtr     = 0.1;
input double     InpSweepPierce  = 0.05;
input int        InpSweepConfirm = 2;
input double     InpFvgMinAtr    = 0.2;
input int        InpFvgMinPoints = 0;
input int        InpFvgMaxAge    = 48;
input TB_MitMode InpFvgMit       = TB_MIT_FULL;

TB_Engine g_eng, g_d1, g_h4, g_h1;
TB_Params g_p;
datetime  g_last = 0;
bool      g_ready = false;
int       g_obj = 0;

string TrendName(const int t)
  {
   if(t == TB_BULL) return "BULL";
   if(t == TB_BEAR) return "BEAR";
   return "RANGE";
  }

color TrendColor(const int t)
  {
   if(t == TB_BULL) return clrLimeGreen;
   if(t == TB_BEAR) return clrTomato;
   return clrSilver;
  }

string NextName()
  {
   g_obj++;
   return "TBA_" + IntegerToString(g_obj);
  }

void BuildParams()
  {
   TB_DefaultParams(g_p);
   g_p.swing_left         = InpSwingLeft;
   g_p.swing_right        = InpSwingRight;
   g_p.min_swing_atr      = InpMinSwingAtr;
   g_p.bos_buffer_atr     = InpBosBufferAtr;
   g_p.disp_body_atr      = InpDispBodyAtr;
   g_p.disp_body_ratio    = InpDispRatio;
   g_p.disp_max_bars      = InpDispMaxBars;
   g_p.eq_tol_atr         = InpEqTolAtr;
   g_p.sweep_pierce_atr   = InpSweepPierce;
   g_p.sweep_confirm_bars = InpSweepConfirm;
   g_p.fvg_min_atr        = InpFvgMinAtr;
   g_p.fvg_min_points     = InpFvgMinPoints;
   g_p.fvg_max_age        = InpFvgMaxAge;
   g_p.fvg_mit_level      = (int)InpFvgMit;
   g_p.point              = SymbolInfoDouble(_Symbol, SYMBOL_POINT);
  }

//--- drawing helpers ------------------------------------------------
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

void Seg(const datetime t1, const double p1, const datetime t2, const double p2,
         const color clr, const ENUM_LINE_STYLE style, const bool ray)
  {
   string nm = NextName();
   if(ObjectCreate(0, nm, OBJ_TREND, 0, t1, p1, t2, p2))
     {
      ObjectSetInteger(0, nm, OBJPROP_COLOR, clr);
      ObjectSetInteger(0, nm, OBJPROP_STYLE, style);
      ObjectSetInteger(0, nm, OBJPROP_RAY_RIGHT, ray);
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

int BarIdx(const long tm)
  {
   int lo = 0, hi = g_eng.m_n - 1, j = -1;
   while(lo <= hi)
     {
      int mid = (lo + hi) / 2;
      if(g_eng.m_tm[mid] <= tm) { j = mid; lo = mid + 1; }
      else hi = mid - 1;
     }
   return j;
  }

int FindSwingByPrice(const int typ, const double price)
  {
   for(int i = g_eng.m_nsw - 1; i >= 0; i--)
      if(g_eng.m_sw[i].stype == typ && g_eng.m_sw[i].price == price)
         return i;
   return -1;
  }

void DrawAll()
  {
   int sec = PeriodSeconds(_Period);
   int n = g_eng.m_n;
   if(n < 2) return;
   datetime lastBar = (datetime)g_eng.m_tm[n - 1];
   int from = MathMax(0, g_eng.m_nev - InpMaxDraw);
   for(int i = from; i < g_eng.m_nev; i++)
     {
      datetime t = (datetime)g_eng.m_ev[i].time;
      string tp = g_eng.m_ev[i].etype;
      int d = g_eng.m_ev[i].dir;
      double pr = g_eng.m_ev[i].price;
      int bi = BarIdx(g_eng.m_ev[i].time);
      if(tp == "SWING")
        {
         int sb = MathMax(0, bi - g_p.swing_right);
         Dot((datetime)g_eng.m_tm[sb], pr, 159, (d == 1) ? clrDodgerBlue : clrOrchid);
        }
      else if(tp == "BOS" || tp == "MSS")
        {
         int si = FindSwingByPrice((d == 1) ? 1 : -1, pr);
         datetime st = (si >= 0) ? (datetime)g_eng.m_tm[g_eng.m_sw[si].idx] : (datetime)(g_eng.m_ev[i].time - 10 * sec);
         color clr = (tp == "BOS") ? TrendColor(d) : clrGold;
         Seg(st, pr, t, pr, clr, (tp == "BOS") ? STYLE_SOLID : STYLE_DASH, false);
         Txt(t, pr, tp, clr);
        }
      else if(tp == "DISP")
        {
         Dot(t, pr, 140, clrYellow);
        }
      else if(tp == "EQ")
        {
         Txt(t, pr, (d == 1) ? "EQH" : "EQL", clrOrange);
        }
      else if(tp == "SWEEP")
        {
         double ext = g_eng.m_ev[i].price2;
         Dot(t, ext, (d == 1) ? 234 : 233, clrOrangeRed);
         Txt(t, ext, "SWEEP " + TB_KindName(g_eng.m_ev[i].flag), clrOrangeRed);
        }
      else if(tp == "FVG")
        {
         double bot = pr, top = g_eng.m_ev[i].price2;
         datetime st = (datetime)g_eng.m_tm[MathMax(0, bi - 2)];
         datetime en = (datetime)(lastBar + 10 * sec);
         for(int j = i + 1; j < g_eng.m_nev; j++)
           {
            if(g_eng.m_ev[j].etype == "FVG_STATE" && g_eng.m_ev[j].price == bot &&
               g_eng.m_ev[j].price2 == top && g_eng.m_ev[j].flag >= 3)
              {
               en = (datetime)(g_eng.m_ev[j].time + sec);
               break;
              }
           }
         Box(st, bot, en, top, (d == 1) ? clrDarkGreen : clrMaroon);
        }
     }
   if(InpDrawLevels)
     {
      for(int k = 0; k < g_eng.m_nlv; k++)
        {
         datetime st = (datetime)g_eng.m_tm[g_eng.m_lv[k].created];
         Seg(st, g_eng.m_lv[k].price, lastBar, g_eng.m_lv[k].price, clrGray, STYLE_DOT, true);
         Txt(lastBar, g_eng.m_lv[k].price, TB_KindName(g_eng.m_lv[k].kind), clrGray);
        }
     }
  }

int LastTrend(TB_Engine &e)
  {
   return (e.m_n > 0) ? e.m_trend_at[e.m_n - 1] : TB_RANGE;
  }

void DrawPanel()
  {
   int d1 = LastTrend(g_d1), h4 = LastTrend(g_h4), h1 = LastTrend(g_h1), ex = LastTrend(g_eng);
   int gate = TB_Gate(d1, h4, InpH4Only);
   string perm = "NO TRADE (trend gate)";
   if(gate == TB_BULL) perm = "long setups only";
   if(gate == TB_BEAR) perm = "short setups only";
   string last = "none";
   if(g_eng.m_nev > 0)
      last = g_eng.m_ev[g_eng.m_nev - 1].etype + " " +
             TimeToString((datetime)g_eng.m_ev[g_eng.m_nev - 1].time, TIME_DATE | TIME_MINUTES);
   Comment("TrendBot Stage A  (SIGNAL-ONLY, no orders)\n",
           "D1: ", TrendName(d1), "   H4: ", TrendName(h4), "   H1: ", TrendName(h1),
           "   ", EnumToString(_Period), ": ", TrendName(ex), "\n",
           "Gate (", InpH4Only ? "H4_ONLY" : "H4_AND_D1", "): ", TrendName(gate), " -> ", perm, "\n",
           "Events: ", g_eng.m_nev, "   Open levels: ", g_eng.m_nlv, "\n",
           "Last event: ", last);
  }

//--- CSV export (for tools/compare_events.py) -----------------------
string Px(const double v)
  {
   return DoubleToString(v, _Digits);
  }

void ExportCsv()
  {
   string base = "TrendBot_" + _Symbol + "_" + EnumToString(_Period);
   int h = FileOpen(base + "_bars.csv", FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(h == INVALID_HANDLE) { Print("TrendBot: cannot write bars csv, err=", GetLastError()); return; }
   FileWrite(h, "#digits=" + IntegerToString(_Digits));
   FileWrite(h, "time", "open", "high", "low", "close");
   for(int i = 0; i < g_eng.m_n; i++)
      FileWrite(h, (string)g_eng.m_tm[i], Px(g_eng.m_o[i]), Px(g_eng.m_h[i]), Px(g_eng.m_l[i]), Px(g_eng.m_c[i]));
   FileClose(h);

   h = FileOpen(base + "_events.csv", FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(h == INVALID_HANDLE) { Print("TrendBot: cannot write events csv, err=", GetLastError()); return; }
   FileWrite(h, "#digits=" + IntegerToString(_Digits));
   FileWrite(h, "#params",
             "swing_left=" + IntegerToString(g_p.swing_left),
             "swing_right=" + IntegerToString(g_p.swing_right),
             "min_swing_atr=" + DoubleToString(g_p.min_swing_atr, 8),
             "bos_buffer_atr=" + DoubleToString(g_p.bos_buffer_atr, 8),
             "disp_body_atr=" + DoubleToString(g_p.disp_body_atr, 8),
             "disp_body_ratio=" + DoubleToString(g_p.disp_body_ratio, 8),
             "disp_max_bars=" + IntegerToString(g_p.disp_max_bars),
             "eq_tol_atr=" + DoubleToString(g_p.eq_tol_atr, 8),
             "sweep_pierce_atr=" + DoubleToString(g_p.sweep_pierce_atr, 8),
             "sweep_confirm_bars=" + IntegerToString(g_p.sweep_confirm_bars),
             "fvg_min_atr=" + DoubleToString(g_p.fvg_min_atr, 8),
             "fvg_min_points=" + IntegerToString(g_p.fvg_min_points),
             "fvg_max_age=" + IntegerToString(g_p.fvg_max_age),
             "fvg_mit_level=" + IntegerToString(g_p.fvg_mit_level),
             "point=" + DoubleToString(g_p.point, 10));
   FileWrite(h, "time", "type", "dir", "price", "price2", "flag", "trend", "gate");
   int sec = PeriodSeconds(_Period);
   for(int i = 0; i < g_eng.m_nev; i++)
     {
      long tc = g_eng.m_ev[i].time + sec;     // bar close time
      int d1 = TB_HtfTrendAt(g_d1, 86400, tc);
      int h4 = TB_HtfTrendAt(g_h4, 14400, tc);
      int gt = TB_Gate(d1, h4, InpH4Only);
      FileWrite(h, (string)g_eng.m_ev[i].time, g_eng.m_ev[i].etype, IntegerToString(g_eng.m_ev[i].dir),
                Px(g_eng.m_ev[i].price), Px(g_eng.m_ev[i].price2), IntegerToString(g_eng.m_ev[i].flag),
                IntegerToString(g_eng.m_ev[i].trend), IntegerToString(gt));
     }
   FileClose(h);
  }

//--- load + analyse ----------------------------------------------------
bool LoadEngine(TB_Engine &e, const ENUM_TIMEFRAMES tf, const int bars)
  {
   MqlRates r[];
   ArraySetAsSeries(r, false);                         // index 0 = oldest
   int got = CopyRates(_Symbol, tf, 1, bars, r);       // start at shift 1: closed bars only
   if(got < 50) return false;
   e.Run(r, got, g_p);
   return true;
  }

bool Rebuild()
  {
   BuildParams();
   ObjectsDeleteAll(0, "TBA_");
   g_obj = 0;
   if(!LoadEngine(g_eng, _Period, InpExecBars))
     {
      Comment("TrendBot Stage A: waiting for ", EnumToString(_Period), " history...");
      return false;
     }
   LoadEngine(g_d1, PERIOD_D1, InpD1Bars);
   LoadEngine(g_h4, PERIOD_H4, InpH4Bars);
   LoadEngine(g_h1, PERIOD_H1, InpH1Bars);
   DrawAll();
   DrawPanel();
   if(InpExportCsv)
      ExportCsv();
   ChartRedraw(0);
   return true;
  }

int OnInit()
  {
   IndicatorSetString(INDICATOR_SHORTNAME, "TrendBot Stage A");
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   ObjectsDeleteAll(0, "TBA_");
   Comment("");
  }

int OnCalculate(const int rates_total, const int prev_calculated, const datetime &time[],
                const double &open[], const double &high[], const double &low[], const double &close[],
                const long &tick_volume[], const long &volume[], const int &spread[])
  {
   datetime cur = iTime(_Symbol, _Period, 0);
   if(g_ready && cur == g_last)
      return rates_total;                               // recompute only when a new bar opens
   g_ready = Rebuild();
   if(g_ready)
      g_last = cur;
   return rates_total;
  }

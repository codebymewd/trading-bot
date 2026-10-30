//+------------------------------------------------------------------+
//| TrendBotIndicators.mqh - indicator confirmation engine           |
//| Mirrors reference/indicators.py (own math, no iRSI/iMACD handles |
//| so results are reproducible). Not yet compiled in MetaEditor.    |
//+------------------------------------------------------------------+
#property strict
#ifndef TB_IND_MQH
#define TB_IND_MQH

#define TB_NA EMPTY_VALUE
#define TB_CONFIRM   1
#define TB_NEUTRAL   0
#define TB_CONFLICT -1

const double TB_SQUEEZE_RATIO    = 0.75;
const double TB_RSI_NEUTRAL_BAND = 5.0;
const int    TB_STOCH_CROSS_BARS = 3;

struct TB_IndParams
  {
   int    rsi_period;
   double rsi_midline;
   int    macd_fast;
   int    macd_slow;
   int    macd_signal;
   int    stoch_k;
   int    stoch_d;
   int    stoch_slowing;
   int    bb_period;
   double bb_deviation;
   int    cci_period;
   double cci_threshold;
  };

void TB_DefaultIndParams(TB_IndParams &p)
  {
   p.rsi_period = 14;  p.rsi_midline = 50.0;
   p.macd_fast = 12;   p.macd_slow = 26;   p.macd_signal = 9;
   p.stoch_k = 14;     p.stoch_d = 3;      p.stoch_slowing = 3;
   p.bb_period = 20;   p.bb_deviation = 2.0;
   p.cci_period = 14;  p.cci_threshold = 100.0;
  }

void TB_Ema(const double &v[], const int n, const int period, double &out[])
  {
   ArrayResize(out, n);
   if(n == 0) return;
   double a = 2.0 / (period + 1);
   out[0] = v[0];
   for(int i = 1; i < n; i++)
      out[i] = a * v[i] + (1.0 - a) * out[i - 1];
  }

void TB_Sma(const double &v[], const int n, const int period, double &out[])
  {
   ArrayResize(out, n);
   for(int i = 0; i < n; i++)
     {
      out[i] = TB_NA;
      if(i < period - 1) continue;
      double s = 0.0;
      bool ok = true;
      for(int k = i - period + 1; k <= i; k++)
        {
         if(v[k] == TB_NA) { ok = false; break; }
         s += v[k];
        }
      if(ok) out[i] = s / period;
     }
  }

void TB_Rsi(const double &c[], const int n, const int period, double &out[])
  {
   ArrayResize(out, n);
   for(int i = 0; i < n; i++) out[i] = TB_NA;
   if(n <= period) return;
   double ag = 0.0, al = 0.0;
   for(int i = 1; i <= period; i++)
     {
      double d = c[i] - c[i - 1];
      ag += MathMax(d, 0.0);
      al += MathMax(-d, 0.0);
     }
   ag /= period;
   al /= period;
   for(int i = period; i < n; i++)
     {
      if(i > period)
        {
         double d = c[i] - c[i - 1];
         ag = (ag * (period - 1) + MathMax(d, 0.0)) / period;
         al = (al * (period - 1) + MathMax(-d, 0.0)) / period;
        }
      if(al == 0.0) out[i] = (ag == 0.0) ? 50.0 : 100.0;
      else          out[i] = 100.0 - 100.0 / (1.0 + ag / al);
     }
  }

void TB_Macd(const double &c[], const int n, const int fast, const int slow, const int signal,
             double &m[], double &s[], double &h[])
  {
   double ef[], es[], line[], sig[];
   TB_Ema(c, n, fast, ef);
   TB_Ema(c, n, slow, es);
   ArrayResize(line, n);
   for(int i = 0; i < n; i++) line[i] = ef[i] - es[i];
   TB_Ema(line, n, signal, sig);
   int start = slow + signal - 2;
   ArrayResize(m, n); ArrayResize(s, n); ArrayResize(h, n);
   for(int i = 0; i < n; i++)
     {
      if(i >= start) { m[i] = line[i]; s[i] = sig[i]; h[i] = line[i] - sig[i]; }
      else           { m[i] = TB_NA;   s[i] = TB_NA;  h[i] = TB_NA; }
     }
  }

void TB_Stoch(const double &h[], const double &l[], const double &c[], const int n,
              const int kp, const int dp, const int slowing, double &k[], double &d[])
  {
   double raw[];
   ArrayResize(raw, n);
   for(int i = 0; i < n; i++) raw[i] = TB_NA;
   for(int i = kp - 1; i < n; i++)
     {
      double hh = h[i - kp + 1], ll = l[i - kp + 1];
      for(int j = i - kp + 2; j <= i; j++)
        { hh = MathMax(hh, h[j]); ll = MathMin(ll, l[j]); }
      raw[i] = (hh == ll) ? 50.0 : 100.0 * (c[i] - ll) / (hh - ll);
     }
   TB_Sma(raw, n, slowing, k);
   TB_Sma(k, n, dp, d);
  }

void TB_Boll(const double &c[], const int n, const int period, const double dev, double &mid[], double &bw[])
  {
   TB_Sma(c, n, period, mid);
   ArrayResize(bw, n);
   for(int i = 0; i < n; i++) bw[i] = TB_NA;
   for(int i = period - 1; i < n; i++)
     {
      double ss = 0.0;
      for(int k = i - period + 1; k <= i; k++)
         ss += (c[k] - mid[i]) * (c[k] - mid[i]);
      double sd = MathSqrt(ss / period);
      bw[i] = (mid[i] != 0.0) ? (2.0 * dev * sd) / mid[i] : 0.0;
     }
  }

void TB_Cci(const double &h[], const double &l[], const double &c[], const int n, const int period, double &out[])
  {
   double tp[];
   ArrayResize(tp, n);
   ArrayResize(out, n);
   for(int i = 0; i < n; i++) { tp[i] = (h[i] + l[i] + c[i]) / 3.0; out[i] = TB_NA; }
   for(int i = period - 1; i < n; i++)
     {
      double m = 0.0;
      for(int k = i - period + 1; k <= i; k++) m += tp[k];
      m /= period;
      double md = 0.0;
      for(int k = i - period + 1; k <= i; k++) md += MathAbs(tp[k] - m);
      md /= period;
      out[i] = (md == 0.0) ? 0.0 : (tp[i] - m) / (0.015 * md);
     }
  }

class TB_IndSeries
  {
public:
   int    n;
   double close[];
   double rsi[];
   double macd[];
   double macd_signal[];
   double macd_hist[];
   double stoch_k[];
   double stoch_d[];
   double bb_mid[];
   double bb_bw[];
   double bb_bw_mean[];
   double cci[];

   void Compute(const double &h[], const double &l[], const double &c[], const int cnt, const TB_IndParams &p)
     {
      n = cnt;
      ArrayResize(close, n);
      for(int i = 0; i < n; i++) close[i] = c[i];
      TB_Rsi(c, n, p.rsi_period, rsi);
      TB_Macd(c, n, p.macd_fast, p.macd_slow, p.macd_signal, macd, macd_signal, macd_hist);
      TB_Stoch(h, l, c, n, p.stoch_k, p.stoch_d, p.stoch_slowing, stoch_k, stoch_d);
      TB_Boll(c, n, p.bb_period, p.bb_deviation, bb_mid, bb_bw);
      TB_Sma(bb_bw, n, p.bb_period, bb_bw_mean);
      TB_Cci(h, l, c, n, p.cci_period, cci);
     }

   double G(const double &a[], const int t)
     {
      if(t < 0 || t >= n) return TB_NA;
      return a[t];
     }
  };

int TB_EvalRsi(TB_IndSeries &s, const int t, const int d, const TB_IndParams &p)
  {
   double r = s.G(s.rsi, t);
   if(r == TB_NA) return TB_NEUTRAL;
   double a = (r - p.rsi_midline) * d;
   if(a > 0) return TB_CONFIRM;
   return (a >= -TB_RSI_NEUTRAL_BAND) ? TB_NEUTRAL : TB_CONFLICT;
  }

int TB_EvalMacd(TB_IndSeries &s, const int t, const int d, const TB_IndParams &p)
  {
   double m = s.G(s.macd, t), h = s.G(s.macd_hist, t), hp = s.G(s.macd_hist, t - 1);
   if(m == TB_NA || h == TB_NA || hp == TB_NA) return TB_NEUTRAL;
   double turning = (h - hp) * d;
   if(turning > 0 && (m * d > 0 || h * d > 0)) return TB_CONFIRM;
   if(turning < 0 && h * d < 0) return TB_CONFLICT;
   return TB_NEUTRAL;
  }

int TB_EvalBb(TB_IndSeries &s, const int t, const int d, const TB_IndParams &p)
  {
   double mid = s.G(s.bb_mid, t), bw = s.G(s.bb_bw, t), bwp = s.G(s.bb_bw, t - 1), mean = s.G(s.bb_bw_mean, t);
   if(mid == TB_NA || bw == TB_NA || bwp == TB_NA || mean == TB_NA) return TB_NEUTRAL;
   double side = (s.close[t] - mid) * d;
   bool expanding = bw > bwp;
   bool squeeze = bw <= TB_SQUEEZE_RATIO * mean;
   if(side > 0 && expanding && !squeeze) return TB_CONFIRM;
   if(side < 0 && expanding) return TB_CONFLICT;
   return TB_NEUTRAL;
  }

int TB_EvalStoch(TB_IndSeries &s, const int t, const int d, const TB_IndParams &p)
  {
   double k = s.G(s.stoch_k, t), dv = s.G(s.stoch_d, t), kp = s.G(s.stoch_k, t - 1);
   if(k == TB_NA || dv == TB_NA || kp == TB_NA) return TB_NEUTRAL;
   double above = (k - dv) * d;
   double rising = (k - kp) * d;
   bool crossed = false;
   for(int u = t - TB_STOCH_CROSS_BARS + 1; u <= t; u++)
     {
      double k0 = s.G(s.stoch_k, u - 1), d0 = s.G(s.stoch_d, u - 1);
      double k1 = s.G(s.stoch_k, u),     d1 = s.G(s.stoch_d, u);
      if(k0 != TB_NA && d0 != TB_NA && k1 != TB_NA && d1 != TB_NA)
         if((k0 - d0) * d <= 0 && (k1 - d1) * d > 0)
            crossed = true;
     }
   if(above > 0 && rising > 0 && crossed) return TB_CONFIRM;
   if(above < 0 && rising < 0) return TB_CONFLICT;
   return TB_NEUTRAL;
  }

int TB_EvalCci(TB_IndSeries &s, const int t, const int d, const TB_IndParams &p)
  {
   double x = s.G(s.cci, t), xp = s.G(s.cci, t - 1);
   if(x == TB_NA || xp == TB_NA) return TB_NEUTRAL;
   if(x * d > 0 && (x - xp) * d > 0) return TB_CONFIRM;
   if(x * d < 0 && (x - xp) * d < 0) return TB_CONFLICT;
   return TB_NEUTRAL;
  }

string TB_Mark(const int st)
  {
   if(st == TB_CONFIRM) return "+";
   if(st == TB_CONFLICT) return "-";
   return "0";
  }

//--- enabled[] order: RSI, MACD, BB, STOCH, CCI. Returns passed; summary matches the Python reference.
bool TB_Confirm(TB_IndSeries &s, const int t, const int d, const bool &enabled[], const int minimum,
                const TB_IndParams &p, string &summary, int &count)
  {
   string names[5] = {"RSI", "MACD", "BB", "STOCH", "CCI"};
   int nen = 0;
   for(int i = 0; i < 5; i++) if(enabled[i]) nen++;
   count = 0;
   summary = "";
   if(minimum < 0 || minimum > nen)
     { summary = "invalid_minimum"; return false; }
   for(int i = 0; i < 5; i++)
     {
      if(!enabled[i]) continue;
      int st = TB_NEUTRAL;
      if(i == 0) st = TB_EvalRsi(s, t, d, p);
      if(i == 1) st = TB_EvalMacd(s, t, d, p);
      if(i == 2) st = TB_EvalBb(s, t, d, p);
      if(i == 3) st = TB_EvalStoch(s, t, d, p);
      if(i == 4) st = TB_EvalCci(s, t, d, p);
      if(st == TB_CONFIRM) count++;
      if(summary != "") summary += " ";
      summary += names[i] + TB_Mark(st);
     }
   summary += " (" + IntegerToString(count) + "/" + IntegerToString(minimum) + ")";
   return count >= minimum;
  }

#endif

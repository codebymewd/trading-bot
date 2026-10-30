//+------------------------------------------------------------------+
//| TrendBotScalp.mqh - M15 setup zones + M5 trigger (SIGNAL ONLY)   |
//| Mirrors reference/scalp.py step by step. Strictly causal: an M15 |
//| event is used only after its M15 bar has closed. No orders here. |
//| Not yet compiled in MetaEditor.                                  |
//+------------------------------------------------------------------+
#property strict
#ifndef TB_SCALP_MQH
#define TB_SCALP_MQH

#include "TrendBotEngine.mqh"
#include "TrendBotIndicators.mqh"

struct TB_ScalpParams
  {
   bool   gate_h4_only;               // false = H4_AND_D1
   bool   fvg_setup;
   bool   sweep_setup;
   bool   require_bos_linked_fvg;
   int    trigger_window_bars;        // M5 bars after the zone is armed
   int    sweep_setup_max_age_bars;   // M15 bars
   int    zone_max_age_bars;          // M15 bars (FVG zones)
   bool   use_premium_discount;
   double max_entry_range_pct;
   double sl_buffer_atr;              // x M15 ATR
   double min_sl_atr;
   double max_sl_atr;
   double max_chase_r;
   double target_multiplier;
   double max_spread_to_stop_ratio;
   double spread;                     // price units
   bool   enabled[5];                 // RSI, MACD, BB, STOCH, CCI
   int    minimum_confirmations;
   int    setup_tf_seconds;
   int    trigger_tf_seconds;
   TB_IndParams ind;
  };

void TB_DefaultScalpParams(TB_ScalpParams &p)
  {
   p.gate_h4_only = false;
   p.fvg_setup = true;
   p.sweep_setup = true;
   p.require_bos_linked_fvg = true;
   p.trigger_window_bars = 12;
   p.sweep_setup_max_age_bars = 12;
   p.zone_max_age_bars = 24;
   p.use_premium_discount = true;
   p.max_entry_range_pct = 50.0;
   p.sl_buffer_atr = 0.1;
   p.min_sl_atr = 0.3;
   p.max_sl_atr = 3.0;
   p.max_chase_r = 0.5;
   p.target_multiplier = 3.0;
   p.max_spread_to_stop_ratio = 0.15;
   p.spread = 0.0;
   p.enabled[0] = true;   // RSI
   p.enabled[1] = true;   // MACD
   p.enabled[2] = true;   // BB
   p.enabled[3] = true;   // STOCH
   p.enabled[4] = false;  // CCI (optional)
   p.minimum_confirmations = 3;
   p.setup_tf_seconds = 900;
   p.trigger_tf_seconds = 300;
   TB_DefaultIndParams(p.ind);
  }

struct TB_Signal
  {
   long   time;        // open time of the M5 bar during which this became known
   string stype;       // SETUP | SETUP_END | CANDIDATE | REJECT
   int    dir;
   double price;
   double price2;
   double price3;
   string reason;
   string detail;
  };

struct TB_Zone
  {
   string kind;        // FVG | SWEEP
   int    dir;
   double low;
   double high;
   double inval;
   int    created_j;
   int    expiry_j;
   int    armed_t;
   int    born_t;
   bool   done;
  };

class TB_Scalp
  {
public:
   TB_ScalpParams m_p;
   TB_Engine     *m_d1;
   TB_Engine     *m_h4;
   TB_Engine     *m_m15;
   TB_Engine     *m_m5;
   TB_IndSeries   m_series;
   bool           m_use_ind;

   TB_Signal      m_sig[];
   int            m_nsig;
   TB_Zone        m_z[];
   int            m_nz;
   int            m_e5;       // first M5 engine event with time >= current M5 bar time

   void Out(const int t, const string typ, const int d, const double price = 0.0, const double price2 = 0.0,
            const double price3 = 0.0, const string reason = "", const string detail = "")
     {
      if(ArraySize(m_sig) < m_nsig + 1)
         ArrayResize(m_sig, m_nsig + 256);
      m_sig[m_nsig].time   = m_m5.m_tm[t];
      m_sig[m_nsig].stype  = typ;
      m_sig[m_nsig].dir    = d;
      m_sig[m_nsig].price  = price;
      m_sig[m_nsig].price2 = price2;
      m_sig[m_nsig].price3 = price3;
      m_sig[m_nsig].reason = reason;
      m_sig[m_nsig].detail = detail;
      m_nsig++;
     }

   //--- largest index with tm[idx] <= x, or -1
   int BisectM15(const long x)
     {
      int lo = 0, hi = m_m15.m_n - 1, j = -1;
      while(lo <= hi)
        {
         int mid = (lo + hi) / 2;
         if(m_m15.m_tm[mid] <= x) { j = mid; lo = mid + 1; }
         else hi = mid - 1;
        }
      return j;
     }

   int ExactM15(const long x)
     {
      int j = BisectM15(x);
      if(j >= 0 && m_m15.m_tm[j] == x) return j;
      return -1;
     }

   void Compact()
     {
      int w = 0;
      for(int k = 0; k < m_nz; k++)
         if(!m_z[k].done)
           {
            if(w != k) m_z[w] = m_z[k];
            w++;
           }
      m_nz = w;
     }

   void AddZone(const string kind, const int dir, const double low, const double high, const double inval,
                const int created_j, const int expiry_j, const int armed_t, const int born_t)
     {
      if(ArraySize(m_z) < m_nz + 1)
         ArrayResize(m_z, m_nz + 32);
      m_z[m_nz].kind = kind;
      m_z[m_nz].dir = dir;
      m_z[m_nz].low = low;
      m_z[m_nz].high = high;
      m_z[m_nz].inval = inval;
      m_z[m_nz].created_j = created_j;
      m_z[m_nz].expiry_j = expiry_j;
      m_z[m_nz].armed_t = armed_t;
      m_z[m_nz].born_t = born_t;
      m_z[m_nz].done = false;
      m_nz++;
     }

   void Run(TB_Engine &d1, TB_Engine &h4, TB_Engine &m15, TB_Engine &m5, const TB_ScalpParams &p)
     {
      m_p = p;
      m_d1 = GetPointer(d1);
      m_h4 = GetPointer(h4);
      m_m15 = GetPointer(m15);
      m_m5 = GetPointer(m5);
      ArrayResize(m_sig, 0);
      ArrayResize(m_z, 0);
      m_nsig = 0;
      m_nz = 0;
      m_e5 = 0;
      int nen = 0;
      for(int i = 0; i < 5; i++) if(p.enabled[i]) nen++;
      m_use_ind = (nen > 0);
      if(m_use_ind)
         m_series.Compute(m5.m_h, m5.m_l, m5.m_c, m5.m_n, p.ind);
      int ptr = 0;
      for(int t = 0; t < m5.m_n; t++)
        {
         long close_t = m5.m_tm[t] + p.trigger_tf_seconds;
         int g = TB_Gate(TB_HtfTrendAt(d1, 86400, close_t), TB_HtfTrendAt(h4, 14400, close_t), p.gate_h4_only);
         while(ptr < m15.m_nev && m15.m_ev[ptr].time + p.setup_tf_seconds <= close_t)
           {
            OnM15Event(ptr, t, g);
            ptr++;
           }
         int j = BisectM15(close_t - p.setup_tf_seconds);
         if(j < 0) continue;
         while(m_e5 < m5.m_nev && m5.m_ev[m_e5].time < m5.m_tm[t])
            m_e5++;
         UpdateZones(t, j, g);
        }
     }

   void OnM15Event(const int ei, const int t, const int g)
     {
      long   etime  = m_m15.m_ev[ei].time;
      string etype  = m_m15.m_ev[ei].etype;
      int    edir   = m_m15.m_ev[ei].dir;
      double eprice = m_m15.m_ev[ei].price;
      double eprice2 = m_m15.m_ev[ei].price2;
      int    eflag  = m_m15.m_ev[ei].flag;
      int j = ExactM15(etime);
      if(j < 0) return;
      if(etype == "FVG" && m_p.fvg_setup && (eflag == 1 || !m_p.require_bos_linked_fvg))
        {
         double low = eprice, high = eprice2;
         double inval = (edir == 1) ? low : high;
         AddZone("FVG", edir, low, high, inval, j, j + m_p.zone_max_age_bars, -1, t);
         Out(t, "SETUP", edir, low, high, g, "FVG");
        }
      else if(etype == "SWEEP" && m_p.sweep_setup)
        {
         int d = -edir;   // lows swept (dir -1) -> long setup; highs swept (dir +1) -> short setup
         double low, high;
         if(d == 1) { low = eprice2; high = eprice; }
         else       { low = eprice;  high = eprice2; }
         AddZone("SWEEP", d, low, high, eprice2, j, j + m_p.sweep_setup_max_age_bars, t, t);
         Out(t, "SETUP", d, low, high, g, "SWEEP");
        }
      else if(etype == "FVG_STATE" && (eflag == 3 || eflag == 4 || eflag == 5))
        {
         string nm = (eflag == 3) ? "filled" : ((eflag == 4) ? "invalid" : "expired");
         for(int k = 0; k < m_nz; k++)
           {
            if(!m_z[k].done && m_z[k].kind == "FVG" && m_z[k].dir == edir &&
               m_z[k].low == eprice && m_z[k].high == eprice2)
              {
               m_z[k].done = true;
               Out(t, "SETUP_END", m_z[k].dir, m_z[k].low, m_z[k].high, 0.0, nm);
              }
           }
        }
      Compact();
     }

   void EndZone(const int k, const int t, const string reason)
     {
      m_z[k].done = true;
      Out(t, "SETUP_END", m_z[k].dir, m_z[k].low, m_z[k].high, 0.0, reason);
     }

   void UpdateZones(const int t, const int j, const int g)
     {
      for(int k = 0; k < m_nz; k++)
        {
         if(m_z[k].done || t <= m_z[k].born_t) continue;
         if(j > m_z[k].expiry_j) { EndZone(k, t, "expired"); continue; }
         double c = m_m5.m_c[t];
         if((m_z[k].dir == 1 && c < m_z[k].inval) || (m_z[k].dir == -1 && c > m_z[k].inval))
           { EndZone(k, t, "invalidated"); continue; }
         if(m_z[k].dir != g) continue;      // trend gate does not allow this direction right now
         if(m_z[k].armed_t < 0)
           {
            bool touched = (m_z[k].dir == 1) ? (m_m5.m_l[t] <= m_z[k].high) : (m_m5.m_h[t] >= m_z[k].low);
            if(touched) m_z[k].armed_t = t;
           }
         if(m_z[k].armed_t < 0) continue;
         if(t - m_z[k].armed_t > m_p.trigger_window_bars) { EndZone(k, t, "trigger_timeout"); continue; }
         if(Triggered(k, t))
            Evaluate(k, t, j);
        }
      Compact();
     }

   bool Triggered(const int k, const int t)
     {
      double o = m_m5.m_o[t], c = m_m5.m_c[t];
      int bd = (c > o) ? 1 : ((c < o) ? -1 : 0);
      if(bd != m_z[k].dir) return false;
      if(m_z[k].kind == "SWEEP")
        {
         if(m_z[k].dir == 1 && c <= m_z[k].high) return false;
         if(m_z[k].dir == -1 && c >= m_z[k].low) return false;
        }
      for(int e = m_e5; e < m_m5.m_nev && m_m5.m_ev[e].time == m_m5.m_tm[t]; e++)
        {
         string tp = m_m5.m_ev[e].etype;
         if((tp == "BOS" || tp == "DISP") && m_m5.m_ev[e].dir == m_z[k].dir)
            return true;
        }
      return false;
     }

   void Evaluate(const int k, const int t, const int j)
     {
      Decide(k, t, j);
      Out(t, "SETUP_END", m_z[k].dir, m_z[k].low, m_z[k].high, 0.0, "consumed");
     }

   void Decide(const int k, const int t, const int j)
     {
      double atr = m_m15.m_atr[j];
      double entry = m_m5.m_c[t];
      int dir = m_z[k].dir;
      double buf = m_p.sl_buffer_atr * atr;
      double sl = (dir == 1) ? m_z[k].inval - buf : m_z[k].inval + buf;
      double risk = MathAbs(entry - sl);
      m_z[k].done = true;
      if(atr <= 0.0 || risk <= 0.0)
        { Out(t, "REJECT", dir, entry, sl, 0.0, "no_atr_or_zero_risk"); return; }
      double proximal = (dir == 1) ? m_z[k].high : m_z[k].low;
      double chase = ((dir == 1) ? (entry - proximal) : (proximal - entry)) / risk;
      string reason = "";
      if(risk < m_p.min_sl_atr * atr)                         reason = "stop_too_tight";
      else if(risk > m_p.max_sl_atr * atr)                    reason = "stop_too_wide";
      else if(m_p.spread > m_p.max_spread_to_stop_ratio * risk) reason = "spread_too_wide";
      else if(chase > m_p.max_chase_r)                        reason = "late_entry_chase";
      else if(!DiscountOk(k, j))                              reason = "premium_discount";
      if(reason != "")
        { Out(t, "REJECT", dir, entry, sl, 0.0, reason); return; }
      string detail = "";
      if(m_use_ind || m_p.minimum_confirmations > 0)
        {
         int count = 0;
         bool pass = TB_Confirm(m_series, t, dir, m_p.enabled, m_p.minimum_confirmations, m_p.ind, detail, count);
         if(!pass)
           { Out(t, "REJECT", dir, entry, sl, 0.0, "indicators_insufficient", detail); return; }
        }
      double target = entry + dir * m_p.target_multiplier * risk;
      Out(t, "CANDIDATE", dir, entry, sl, target, m_z[k].kind, detail);
     }

   bool DiscountOk(const int k, const int j)
     {
      if(!m_p.use_premium_discount) return true;
      double sh = m_m15.m_sh_at[j], sl = m_m15.m_sl_at[j];
      if(sh == EMPTY_VALUE || sl == EMPTY_VALUE || sh <= sl) return true;
      double mid = (m_z[k].low + m_z[k].high) / 2.0;
      double pos = (m_z[k].dir == 1) ? (mid - sl) / (sh - sl) * 100.0 : (sh - mid) / (sh - sl) * 100.0;
      return pos <= m_p.max_entry_range_pct;
     }
  };

#endif

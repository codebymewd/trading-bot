//+------------------------------------------------------------------+
//| TrendBotEngine.mqh - Stage A detection engine (swings, BOS/MSS,  |
//| displacement, FVG, liquidity + sweeps). Mirrors the Python       |
//| reference reference/engine.py step by step. Strictly causal:     |
//| Step(t) reads bars <= t only. Not yet compiled in MetaEditor.    |
//+------------------------------------------------------------------+
#property strict
#ifndef TB_ENGINE_MQH
#define TB_ENGINE_MQH

#define TB_BULL   1
#define TB_BEAR  -1
#define TB_RANGE  0
#define TB_LEVEL_CAP 30

// level kinds (same codes as the Python reference)
#define TBK_PDH 1
#define TBK_PDL 2
#define TBK_PWH 3
#define TBK_PWL 4
#define TBK_SWH 5
#define TBK_SWL 6
#define TBK_EQH 7
#define TBK_EQL 8

string TB_KindName(const int k)
  {
   switch(k)
     {
      case 1:  return "PDH";
      case 2:  return "PDL";
      case 3:  return "PWH";
      case 4:  return "PWL";
      case 5:  return "SWH";
      case 6:  return "SWL";
      case 7:  return "EQH";
      case 8:  return "EQL";
      case 9:  return "ASH";
      case 10: return "ASL";
      case 11: return "LOH";
      case 12: return "LOL";
      case 13: return "NYH";
      case 14: return "NYL";
     }
   return "?";
  }

struct TB_Params
  {
   int    atr_period;
   int    swing_left;
   int    swing_right;
   double min_swing_atr;
   double bos_buffer_atr;
   double disp_body_atr;
   double disp_body_ratio;
   int    disp_max_bars;
   double eq_tol_atr;
   double sweep_pierce_atr;
   int    sweep_confirm_bars;
   double fvg_min_atr;
   int    fvg_min_points;
   int    fvg_max_age;
   int    fvg_mit_level;   // 1 touch, 2 half, 3 full
   double point;
  };

void TB_DefaultParams(TB_Params &p)
  {
   p.atr_period         = 14;
   p.swing_left         = 3;
   p.swing_right        = 3;
   p.min_swing_atr      = 0.5;
   p.bos_buffer_atr     = 0.05;
   p.disp_body_atr      = 1.2;
   p.disp_body_ratio    = 0.6;
   p.disp_max_bars      = 3;
   p.eq_tol_atr         = 0.1;
   p.sweep_pierce_atr   = 0.05;
   p.sweep_confirm_bars = 2;
   p.fvg_min_atr        = 0.2;
   p.fvg_min_points     = 0;
   p.fvg_max_age        = 48;
   p.fvg_mit_level      = 3;
   p.point              = 0.0;
  }

struct TB_Event
  {
   long   time;
   string etype;
   int    dir;
   double price;
   double price2;
   int    flag;
   int    trend;
  };

struct TB_Swing
  {
   int    stype;     // +1 high, -1 low
   int    idx;
   double price;
   int    confirm;
   bool   broken;
  };

struct TB_Level
  {
   int    kind;
   int    side;
   double price;
   int    created;
   int    state;     // 0 open, 1 pending confirmation
   int    pierce;
   double extreme;
   bool   dead;
  };

struct TB_Fvg
  {
   int    dir;
   int    created;
   double bot;
   double top;
   int    state;     // 0 active 1 touched 2 half 3 filled 4 invalid 5 expired
   bool   closed;
  };

class TB_Engine
  {
public:
   TB_Params m_p;
   int       m_n;
   long      m_tm[];
   double    m_o[];
   double    m_h[];
   double    m_l[];
   double    m_c[];
   double    m_tr[];
   double    m_atr[];
   int       m_trend_at[];
   double    m_sh_at[];      // last confirmed swing high known at bar t (EMPTY_VALUE = none)
   double    m_sl_at[];

   TB_Event  m_ev[];
   int       m_nev;
   TB_Swing  m_sw[];
   int       m_nsw;
   TB_Level  m_lv[];
   int       m_nlv;
   TB_Fvg    m_fv[];
   int       m_nfv;

   int       m_trend;
   bool      m_lock;
   int       m_lock_idx;
   int       m_last_bos_dir;
   int       m_last_bos_idx;
   int       m_bos_bull;
   int       m_bos_bear;
   int       m_last_disp_idx;
   int       m_last_disp_dir;
   bool      m_have_ctx;
   long      m_cur_day;
   double    m_day_h, m_day_l;
   long      m_cur_week;
   double    m_week_h, m_week_l;
   long      m_sess_key;
   double    m_sess_h, m_sess_l;

   TB_Engine()
     {
      m_n = 0;
      TB_DefaultParams(m_p);
      Reset();
     }

   //--- run the whole series (oldest bar first, closed bars only)
   void Run(const MqlRates &r[], const int count, const TB_Params &params)
     {
      m_p = params;
      m_n = count;
      ArrayResize(m_tm, count);
      ArrayResize(m_o, count);
      ArrayResize(m_h, count);
      ArrayResize(m_l, count);
      ArrayResize(m_c, count);
      ArrayResize(m_tr, count);
      ArrayResize(m_atr, count);
      ArrayResize(m_trend_at, count);
      ArrayResize(m_sh_at, count);
      ArrayResize(m_sl_at, count);
      for(int i = 0; i < count; i++)
        {
         m_tm[i] = (long)r[i].time;
         m_o[i]  = r[i].open;
         m_h[i]  = r[i].high;
         m_l[i]  = r[i].low;
         m_c[i]  = r[i].close;
        }
      Reset();
      for(int t = 0; t < m_n; t++)
         Step(t);
     }

   void Reset()
     {
      ArrayResize(m_ev, 0);
      ArrayResize(m_sw, 0);
      ArrayResize(m_lv, 0);
      ArrayResize(m_fv, 0);
      m_nev = 0; m_nsw = 0; m_nlv = 0; m_nfv = 0;
      m_trend = TB_RANGE;
      m_lock = false;
      m_lock_idx = -1;
      m_last_bos_dir = 0;
      m_last_bos_idx = -1;
      m_bos_bull = -99;
      m_bos_bear = -99;
      m_last_disp_idx = -99;
      m_last_disp_dir = 0;
      m_have_ctx = false;
      m_cur_day = 0; m_cur_week = 0;
      m_day_h = 0; m_day_l = 0; m_week_h = 0; m_week_l = 0;
      m_sess_key = -1;
      m_sess_h = 0; m_sess_l = 0;
     }

   //--- helpers
   void Emit(const int t, const string typ, const int d, const double price,
             const double price2 = 0.0, const int flag = 0)
     {
      if(ArraySize(m_ev) < m_nev + 1)
         ArrayResize(m_ev, m_nev + 512);
      m_ev[m_nev].time   = m_tm[t];
      m_ev[m_nev].etype  = typ;
      m_ev[m_nev].dir    = d;
      m_ev[m_nev].price  = price;
      m_ev[m_nev].price2 = price2;
      m_ev[m_nev].flag   = flag;
      m_ev[m_nev].trend  = 0;
      m_nev++;
     }

   int Dir(const int i)
     {
      if(m_c[i] > m_o[i]) return 1;
      if(m_c[i] < m_o[i]) return -1;
      return 0;
     }

   double Body(const int i) { return MathAbs(m_c[i] - m_o[i]); }
   double Rng(const int i)  { return m_h[i] - m_l[i]; }

   int LastSwing(const int typ)
     {
      for(int i = m_nsw - 1; i >= 0; i--)
         if(m_sw[i].stype == typ)
            return i;
      return -1;
     }

   bool IsSingleKind(const int k)
     {
      return (k >= 1 && k <= 4) || (k >= 9 && k <= 14);
     }

   void RemoveLevelAt(const int idx)
     {
      for(int i = idx; i < m_nlv - 1; i++)
         m_lv[i] = m_lv[i + 1];
      m_nlv--;
     }

   void AddLevel(const int kind, const int side, const double price, const int created)
     {
      if(IsSingleKind(kind))
        {
         int w = 0;
         for(int i = 0; i < m_nlv; i++)
            if(m_lv[i].kind != kind)
              {
               if(w != i) m_lv[w] = m_lv[i];
               w++;
              }
         m_nlv = w;
        }
      else
        {
         int cnt = 0, first = -1;
         for(int i = 0; i < m_nlv; i++)
            if(m_lv[i].kind == kind)
              {
               if(first < 0) first = i;
               cnt++;
              }
         if(cnt >= TB_LEVEL_CAP && first >= 0)
            RemoveLevelAt(first);
        }
      if(ArraySize(m_lv) < m_nlv + 1)
         ArrayResize(m_lv, m_nlv + 32);
      TB_Level L;
      L.kind = kind; L.side = side; L.price = price; L.created = created;
      L.state = 0; L.pierce = -1; L.extreme = price; L.dead = false;
      m_lv[m_nlv] = L;
      m_nlv++;
     }

   //--- one bar
   void Step(const int t)
     {
      int evStart = m_nev;
      // 1. ATR = simple mean of true range
      double tr = m_h[t] - m_l[t];
      if(t > 0)
         tr = MathMax(m_h[t] - m_l[t], MathMax(MathAbs(m_h[t] - m_c[t - 1]), MathAbs(m_l[t] - m_c[t - 1])));
      m_tr[t] = tr;
      double atr = 0.0;
      if(t >= m_p.atr_period - 1)
        {
         double s = 0.0;
         for(int j = t - m_p.atr_period + 1; j <= t; j++)
            s += m_tr[j];
         atr = s / m_p.atr_period;
        }
      m_atr[t] = atr;
      // 2. calendar context
      UpdateContext(t);
      if(atr <= 0.0)
        {
         m_trend_at[t] = TB_RANGE;
         m_sh_at[t] = EMPTY_VALUE;
         m_sl_at[t] = EMPTY_VALUE;
         return;
        }
      // 3. swings
      int R = m_p.swing_right, L = m_p.swing_left;
      int i = t - R;
      if(i >= L)
        {
         double mxL = -DBL_MAX, mxR = -DBL_MAX, mnL = DBL_MAX, mnR = DBL_MAX;
         for(int k = i - L; k < i; k++)
           { mxL = MathMax(mxL, m_h[k]); mnL = MathMin(mnL, m_l[k]); }
         for(int k = i + 1; k <= i + R; k++)
           { mxR = MathMax(mxR, m_h[k]); mnR = MathMin(mnR, m_l[k]); }
         if(m_h[i] > mxL && m_h[i] > mxR)
            AddSwing(1, i, m_h[i], t);
         if(m_l[i] < mnL && m_l[i] < mnR)
            AddSwing(-1, i, m_l[i], t);
        }
      int ih = LastSwing(1);
      int il = LastSwing(-1);
      m_sh_at[t] = (ih >= 0) ? m_sw[ih].price : EMPTY_VALUE;
      m_sl_at[t] = (il >= 0) ? m_sw[il].price : EMPTY_VALUE;
      double buf = m_p.bos_buffer_atr * atr;
      // 4. MSS (previous bar's trend), then BOS
      if(m_trend == TB_BULL && il >= 0 && m_c[t] < m_sw[il].price - buf)
        {
         Emit(t, "MSS", -1, m_sw[il].price);
         m_lock = true; m_lock_idx = t;
        }
      else if(m_trend == TB_BEAR && ih >= 0 && m_c[t] > m_sw[ih].price + buf)
        {
         Emit(t, "MSS", 1, m_sw[ih].price);
         m_lock = true; m_lock_idx = t;
        }
      if(ih >= 0 && !m_sw[ih].broken && m_c[t] > m_sw[ih].price + buf)
        {
         m_sw[ih].broken = true;
         Emit(t, "BOS", 1, m_sw[ih].price);
         m_last_bos_dir = 1; m_last_bos_idx = t; m_bos_bull = t;
        }
      if(il >= 0 && !m_sw[il].broken && m_c[t] < m_sw[il].price - buf)
        {
         m_sw[il].broken = true;
         Emit(t, "BOS", -1, m_sw[il].price);
         m_last_bos_dir = -1; m_last_bos_idx = t; m_bos_bear = t;
        }
      // 5. trend from the last two swing highs / lows
      double hp1 = 0, hp2 = 0, lp1 = 0, lp2 = 0;   // hp2 = latest, hp1 = previous
      int nh = 0, nl = 0;
      for(int k = m_nsw - 1; k >= 0; k--)
        {
         if(m_sw[k].stype == 1 && nh < 2)
           { if(nh == 0) hp2 = m_sw[k].price; else hp1 = m_sw[k].price; nh++; }
         if(m_sw[k].stype == -1 && nl < 2)
           { if(nl == 0) lp2 = m_sw[k].price; else lp1 = m_sw[k].price; nl++; }
         if(nh >= 2 && nl >= 2) break;
        }
      int raw = TB_RANGE;
      if(nh == 2 && nl == 2)
        {
         if(hp2 > hp1 && lp2 > lp1) raw = TB_BULL;
         else if(hp2 < hp1 && lp2 < lp1) raw = TB_BEAR;
        }
      int eff = raw;
      if(m_lock)
        {
         if(raw != TB_RANGE && m_last_bos_dir == raw && m_last_bos_idx > m_lock_idx)
            m_lock = false;
         else
            eff = TB_RANGE;
        }
      if(eff != m_trend)
        {
         m_trend = eff;
         Emit(t, "TREND", eff, m_c[t]);
        }
      m_trend_at[t] = m_trend;
      // 6. displacement
      int d = Dir(t);
      if(d != 0)
        {
         for(int k = 1; k <= m_p.disp_max_bars; k++)
           {
            int s0 = t - k + 1;
            if(s0 < 0) break;
            bool ok = true;
            for(int j = s0; j <= t; j++)
              {
               if(Dir(j) != d || Rng(j) <= 0.0 || Body(j) / Rng(j) < m_p.disp_body_ratio)
                 { ok = false; break; }
              }
            if(!ok) break;
            if(MathAbs(m_c[t] - m_o[s0]) >= m_p.disp_body_atr * atr)
              {
               if(!(m_last_disp_dir == d && m_last_disp_idx == t - 1))
                  Emit(t, "DISP", d, m_c[t], m_o[s0], k);
               m_last_disp_idx = t; m_last_disp_dir = d;
               break;
              }
           }
        }
      // 7. FVG
      FvgCreate(t, atr);
      FvgUpdate(t);
      // 8. sweeps
      LevelCheck(t, atr);
      for(int k = evStart; k < m_nev; k++)
         m_ev[k].trend = m_trend;
     }

   void UpdateContext(const int t)
     {
      double h = m_h[t], l = m_l[t];
      long day  = m_tm[t] / 86400;
      long week = (day + 3) / 7;
      if(!m_have_ctx)
        {
         m_have_ctx = true;
         m_cur_day = day; m_day_h = h; m_day_l = l;
         m_cur_week = week; m_week_h = h; m_week_l = l;
        }
      else
        {
         if(day != m_cur_day)
           {
            AddLevel(TBK_PDH, 1, m_day_h, t);
            AddLevel(TBK_PDL, -1, m_day_l, t);
            m_cur_day = day; m_day_h = h; m_day_l = l;
           }
         else
           { m_day_h = MathMax(m_day_h, h); m_day_l = MathMin(m_day_l, l); }
         if(week != m_cur_week)
           {
            AddLevel(TBK_PWH, 1, m_week_h, t);
            AddLevel(TBK_PWL, -1, m_week_l, t);
            m_cur_week = week; m_week_h = h; m_week_l = l;
           }
         else
           { m_week_h = MathMax(m_week_h, h); m_week_l = MathMin(m_week_l, l); }
        }
      int hour = (int)((m_tm[t] % 86400) / 3600);
      int s = -1;                                   // sessions: AS 0-8, LO 8-13, NY 13-21 (server time)
      if(hour >= 0 && hour < 8)       s = 0;
      else if(hour >= 8 && hour < 13) s = 1;
      else if(hour >= 13 && hour < 21) s = 2;
      long key = (s >= 0) ? day * 4 + s : -1;
      if(key != m_sess_key)
        {
         if(m_sess_key >= 0)
           {
            int ps = (int)(m_sess_key % 4);
            AddLevel(9 + 2 * ps, 1, m_sess_h, t);
            AddLevel(10 + 2 * ps, -1, m_sess_l, t);
           }
         m_sess_key = key;
         m_sess_h = h; m_sess_l = l;
        }
      else if(key >= 0)
        {
         m_sess_h = MathMax(m_sess_h, h);
         m_sess_l = MathMin(m_sess_l, l);
        }
     }

   void AddSwing(const int typ, const int idx, const double price, const int t)
     {
      int io = LastSwing(-typ);
      if(io >= 0 && MathAbs(price - m_sw[io].price) < m_p.min_swing_atr * m_atr[t])
         return;
      int kind = (typ == 1) ? TBK_SWH : TBK_SWL;
      bool haveLast = (m_nsw > 0);
      if(haveLast && m_sw[m_nsw - 1].stype == typ)
        {
         bool better = (typ == 1 && price > m_sw[m_nsw - 1].price) || (typ == -1 && price < m_sw[m_nsw - 1].price);
         if(!better)
            return;
         for(int i = 0; i < m_nlv; i++)
            if(m_lv[i].kind == kind && m_lv[i].price == m_sw[m_nsw - 1].price && m_lv[i].created == m_sw[m_nsw - 1].confirm)
              { RemoveLevelAt(i); break; }
         m_sw[m_nsw - 1].idx = idx;
         m_sw[m_nsw - 1].price = price;
         m_sw[m_nsw - 1].confirm = t;
         m_sw[m_nsw - 1].broken = false;
        }
      else
        {
         if(ArraySize(m_sw) < m_nsw + 1)
            ArrayResize(m_sw, m_nsw + 128);
         m_sw[m_nsw].stype = typ;
         m_sw[m_nsw].idx = idx;
         m_sw[m_nsw].price = price;
         m_sw[m_nsw].confirm = t;
         m_sw[m_nsw].broken = false;
         m_nsw++;
        }
      Emit(t, "SWING", typ, price);
      AddLevel(kind, typ, price, t);
      int best = -1;
      double bd = 0.0, tol = m_p.eq_tol_atr * m_atr[t];
      for(int i = 0; i < m_nlv; i++)
        {
         if(m_lv[i].kind == kind && !(m_lv[i].created == t && m_lv[i].price == price))
           {
            double dd = MathAbs(m_lv[i].price - price);
            if(dd <= tol && (best < 0 || dd < bd))
              { best = i; bd = dd; }
           }
        }
      if(best >= 0)
        {
         double lvl = (typ == 1) ? MathMax(price, m_lv[best].price) : MathMin(price, m_lv[best].price);
         AddLevel((typ == 1) ? TBK_EQH : TBK_EQL, typ, lvl, t);
         Emit(t, "EQ", typ, lvl);
        }
     }

   void FvgCreate(const int t, const double atr)
     {
      if(t < 2) return;
      int dirn = 0;
      double bot = 0, top = 0;
      if(m_l[t] > m_h[t - 2])      { dirn = 1;  bot = m_h[t - 2]; top = m_l[t]; }
      else if(m_h[t] < m_l[t - 2]) { dirn = -1; bot = m_h[t];     top = m_l[t - 2]; }
      if(dirn == 0) return;
      double r2 = Rng(t - 1);
      bool c2disp = (r2 > 0.0 && Body(t - 1) >= m_p.disp_body_atr * atr && Body(t - 1) / r2 >= m_p.disp_body_ratio);
      double size = top - bot;
      double minSize = MathMax(m_p.fvg_min_atr * atr, m_p.fvg_min_points * m_p.point);
      if(size >= minSize && c2disp && Dir(t - 1) == dirn)
        {
         int bosIdx = (dirn == 1) ? m_bos_bull : m_bos_bear;
         int linked = (bosIdx >= t - 2) ? 1 : 0;
         if(ArraySize(m_fv) < m_nfv + 1)
            ArrayResize(m_fv, m_nfv + 32);
         m_fv[m_nfv].dir = dirn; m_fv[m_nfv].created = t;
         m_fv[m_nfv].bot = bot; m_fv[m_nfv].top = top;
         m_fv[m_nfv].state = 0; m_fv[m_nfv].closed = false;
         m_nfv++;
         Emit(t, "FVG", dirn, bot, top, linked);
        }
     }

   void FvgUpdate(const int t)
     {
      for(int k = 0; k < m_nfv; k++)
        {
         if(m_fv[k].closed || m_fv[k].created == t) continue;
         double mid = (m_fv[k].bot + m_fv[k].top) / 2.0;
         int comp = 0;
         if(m_fv[k].dir == 1)
           {
            if(m_c[t] < m_fv[k].bot) comp = 4;
            else if(m_l[t] <= m_fv[k].bot) comp = 3;
            else if(m_l[t] <= mid) comp = 2;
            else if(m_l[t] <= m_fv[k].top) comp = 1;
           }
         else
           {
            if(m_c[t] > m_fv[k].top) comp = 4;
            else if(m_h[t] >= m_fv[k].top) comp = 3;
            else if(m_h[t] >= mid) comp = 2;
            else if(m_h[t] >= m_fv[k].bot) comp = 1;
           }
         int nw = (comp == 4) ? 4 : MathMax(m_fv[k].state, comp);
         if(nw != m_fv[k].state)
           {
            m_fv[k].state = nw;
            Emit(t, "FVG_STATE", m_fv[k].dir, m_fv[k].bot, m_fv[k].top, nw);
           }
         if(m_fv[k].state == 4 || m_fv[k].state >= m_p.fvg_mit_level)
            m_fv[k].closed = true;
         else if(t - m_fv[k].created > m_p.fvg_max_age)
           {
            m_fv[k].state = 5;
            Emit(t, "FVG_STATE", m_fv[k].dir, m_fv[k].bot, m_fv[k].top, 5);
            m_fv[k].closed = true;
           }
        }
      int w = 0;
      for(int k = 0; k < m_nfv; k++)
         if(!m_fv[k].closed)
           {
            if(w != k) m_fv[w] = m_fv[k];
            w++;
           }
      m_nfv = w;
     }

   void Sweep(const int t, const int k)
     {
      Emit(t, "SWEEP", m_lv[k].side, m_lv[k].price, m_lv[k].extreme, m_lv[k].kind);
      m_lv[k].dead = true;
     }

   void LevelCheck(const int t, const double atr)
     {
      double buf = m_p.sweep_pierce_atr * atr;
      for(int k = 0; k < m_nlv; k++)
        {
         if(m_lv[k].side == 1)
           {
            if(m_lv[k].state == 0)
              {
               if(m_h[t] > m_lv[k].price + buf)
                 {
                  m_lv[k].extreme = m_h[t];
                  if(m_c[t] < m_lv[k].price) Sweep(t, k);
                  else if(m_p.sweep_confirm_bars <= 1) m_lv[k].dead = true;
                  else { m_lv[k].state = 1; m_lv[k].pierce = t; }
                 }
              }
            else if(m_lv[k].state == 1)
              {
               m_lv[k].extreme = MathMax(m_lv[k].extreme, m_h[t]);
               if(m_c[t] < m_lv[k].price) Sweep(t, k);
               else if(t - m_lv[k].pierce >= m_p.sweep_confirm_bars - 1) m_lv[k].dead = true;
              }
           }
         else
           {
            if(m_lv[k].state == 0)
              {
               if(m_l[t] < m_lv[k].price - buf)
                 {
                  m_lv[k].extreme = m_l[t];
                  if(m_c[t] > m_lv[k].price) Sweep(t, k);
                  else if(m_p.sweep_confirm_bars <= 1) m_lv[k].dead = true;
                  else { m_lv[k].state = 1; m_lv[k].pierce = t; }
                 }
              }
            else if(m_lv[k].state == 1)
              {
               m_lv[k].extreme = MathMin(m_lv[k].extreme, m_l[t]);
               if(m_c[t] > m_lv[k].price) Sweep(t, k);
               else if(t - m_lv[k].pierce >= m_p.sweep_confirm_bars - 1) m_lv[k].dead = true;
              }
           }
        }
      int w = 0;
      for(int k = 0; k < m_nlv; k++)
         if(!m_lv[k].dead)
           {
            if(w != k) m_lv[w] = m_lv[k];
            w++;
           }
      m_nlv = w;
     }
  };

//--- trend of the latest higher-TF bar that is fully closed at t_close (no look-ahead)
int TB_HtfTrendAt(TB_Engine &e, const int tf_seconds, const long t_close)
  {
   long lim = t_close - tf_seconds;
   int lo = 0, hi = e.m_n - 1, j = -1;
   while(lo <= hi)
     {
      int mid = (lo + hi) / 2;
      if(e.m_tm[mid] <= lim) { j = mid; lo = mid + 1; }
      else hi = mid - 1;
     }
   return (j >= 0) ? e.m_trend_at[j] : TB_RANGE;
  }

int TB_Gate(const int d1, const int h4, const bool h4_only)
  {
   if(h4_only) return h4;
   return (d1 == h4) ? d1 : TB_RANGE;
  }

#endif

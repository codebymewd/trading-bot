//+------------------------------------------------------------------+
//| TestTrendBotCore.mq5 - run from MT5 Navigator > Scripts          |
//| Checks the EA core against the SAME vectors the Python tests use |
//| (generated into TrendBotContract.mqh from config/contract.json). |
//| Result is printed in the Experts tab: look for "ALL PASSED".     |
//+------------------------------------------------------------------+
#property strict
#property script_show_inputs
#include <TrendBot/TrendBotCore.mqh>

int g_fail = 0;

void Check(const bool ok, const string name)
  {
   if(!ok)
     {
      g_fail++;
      Print("FAIL: ", name);
     }
  }

void SetField(TB_Config &c, const string field, const string v)
  {
   if(field == "target_multiplier")            c.target_multiplier = StringToDouble(v);
   else if(field == "scalp_window_minutes")    c.scalp_window_minutes = (int)StringToInteger(v);
   else if(field == "timeout_action")          c.timeout_action = v;
   else if(field == "max_positions_per_basket") c.max_positions_per_basket = (int)StringToInteger(v);
   else if(field == "minimum_confirmations")   c.min_confirmations = (int)StringToInteger(v);
   else if(field == "setup_tf")                c.setup_tf = v;
   else if(field == "risk_percent")            c.risk_percent = StringToDouble(v);
   else if(field == "max_simultaneous_positions") c.max_positions = (int)StringToInteger(v);
   else if(field == "max_trades_per_day")      c.max_trades_per_day = (int)StringToInteger(v);
   else if(field == "entry_mode")              c.entry_mode = v;
   else if(field == "htf_mode")                c.htf_mode = v;
   else if(field == "execution_tf")            c.execution_tf = v;
   else if(field == "mode")                    c.mode = v;
   else Print("WARNING: unmapped field ", field);
  }

void OnStart()
  {
   string err;

   // 1. defaults valid and equal to the agreed values
   TB_Config c;
   TB_LoadDefaults(c);
   Check(TB_Validate(c, err), "defaults valid: " + err);
   Check(c.mode == "SIGNAL_ONLY", "default mode SIGNAL_ONLY");
   Check(c.risk_percent == 1.0 && c.max_open_risk_percent == 1.0, "default risk 1%");
   Check(c.max_positions == 1, "default positions 1");
   Check(c.target_multiplier == 3.0, "default target multiplier 3x");
   Check(c.setup_tf == "M15" && c.execution_tf == "M5", "default M15 setup / M5 trigger");
   Check(c.scalp_window_minutes == 15 && c.timeout_action == "CLOSE_ALL", "default scalp window + timeout");
   Check(c.max_positions_per_basket == 1 && c.min_confirmations == 3, "default basket positions + confirmations");
   Check(c.htf_mode == "H4_AND_D1", "default htf");
   Check(c.entry_mode == "market_confirmed", "default entry");
   Check(!c.sweep_required, "sweep optional");
   Check(c.reset_utc_offset_seconds == 3600 && c.reset_minutes == 0, "Lagos reset");
   Check(!TB_OrdersAllowed(c), "SIGNAL_ONLY must not allow orders");

   // 2. invalid cases shared with Python
   for(int i = 0; i < TB_INVALID_COUNT; i++)
     {
      TB_Config t;
      TB_LoadDefaults(t);
      SetField(t, TB_INVALID_FIELD[i], TB_INVALID_VALUE[i]);
      Check(!TB_Validate(t, err), "should reject: " + TB_INVALID_FIELD[i] + "=" + TB_INVALID_VALUE[i]);
     }

   // 3. LIVE needs confirmation
   TB_Config l;
   TB_LoadDefaults(l);
   l.mode = "LIVE";
   Check(!TB_Validate(l, err), "LIVE without confirmation rejected");
   l.live_confirmed = true;
   Check(TB_OrdersAllowed(l), "LIVE confirmed allowed");

   // 4. trading-day golden vectors
   for(int i = 0; i < TB_VEC_COUNT; i++)
      Check(TB_TradingDayId(TB_VEC_EPOCH[i], TB_VEC_OFFSET[i], TB_VEC_RESET_MIN[i]) == TB_VEC_DAY[i],
            "trading day vector " + (string)i);

   // 5. daily roll: same day keeps, next day resets, earlier day never resets
   TB_Daily d;
   d.day = 100; d.trades = 2; d.losses = 2; d.consecutive_losses = 2; d.locked_out = true; d.clock_anomaly = false;
   Check(!TB_DailyRoll(d, 100) && d.trades == 2 && d.locked_out, "same day keeps");
   Check(!TB_DailyRoll(d, 99) && d.trades == 2 && d.locked_out && d.clock_anomaly, "backwards clock keeps + flags");
   Check(TB_DailyRoll(d, 101) && d.trades == 0 && !d.locked_out && d.consecutive_losses == 2, "next day resets daily only");

   Print(g_fail == 0 ? "TrendBotCore tests: ALL PASSED" : "TrendBotCore tests: FAILURES = " + (string)g_fail);
  }

//+------------------------------------------------------------------+
//| TrendBotCore.mqh - EA-side validation, trading day, daily state  |
//| Mirrors backend/app/config_schema.py + backend/app/trading_day.py |
//| NOTE: not yet compiled in MetaEditor. Compile, then run the      |
//| script Scripts/TestTrendBotCore.mq5 and check the Experts log.   |
//+------------------------------------------------------------------+
#property strict
#ifndef TB_CORE_MQH
#define TB_CORE_MQH

#include "TrendBotContract.mqh"

struct TB_Config
  {
   string mode;
   bool   live_confirmed;
   double risk_percent;
   double max_open_risk_percent;
   int    max_positions;
   double target_multiplier;          // basket profit target = basket risk x multiplier
   string htf_mode;
   string entry_mode;
   string execution_tf;               // scalp trigger timeframe
   string setup_tf;                   // setup / location timeframe
   int    scalp_window_minutes;
   string timeout_action;
   int    max_positions_per_basket;
   int    min_confirmations;
   int    enabled_indicator_count;
   bool   sweep_required;
   int    reset_utc_offset_seconds;   // resolved by backend (Africa/Lagos = 3600)
   int    reset_minutes;              // 00:00 = 0
   int    max_trades_per_day;
   int    lockout_after_losses;
  };

//--- defaults identical to config/contract.json "defaults"
void TB_LoadDefaults(TB_Config &c)
  {
   c.mode                     = TB_DEF_MODE;
   c.live_confirmed           = false;
   c.risk_percent             = TB_DEF_RISK;
   c.max_open_risk_percent    = TB_DEF_OPEN_RISK;
   c.max_positions            = TB_DEF_POSITIONS;
   c.target_multiplier        = TB_DEF_TARGET_MULT;
   c.htf_mode                 = TB_DEF_HTF_MODE;
   c.entry_mode               = TB_DEF_ENTRY_MODE;
   c.execution_tf             = TB_DEF_EXEC_TF;
   c.setup_tf                 = TB_DEF_SETUP_TF;
   c.scalp_window_minutes     = TB_DEF_WINDOW;
   c.timeout_action           = TB_DEF_TIMEOUT;
   c.max_positions_per_basket = TB_DEF_BASKET_POS;
   c.min_confirmations        = TB_DEF_MIN_CONF;
   c.enabled_indicator_count  = TB_DEF_ENABLED_COUNT;
   c.sweep_required           = TB_DEF_SWEEP_REQUIRED;
   c.reset_utc_offset_seconds = TB_DEF_RESET_OFFSET;
   c.reset_minutes            = TB_DEF_RESET_MINUTES;
   c.max_trades_per_day       = TB_DEF_MAX_TRADES;
   c.lockout_after_losses     = TB_DEF_LOCKOUT_LOSSES;
  }

bool TB_InList(const string v, const string &list[])
  {
   for(int i = 0; i < ArraySize(list); i++)
      if(list[i] == v)
         return true;
   return false;
  }

//--- independent validation; returns false and a reason on the first failure
bool TB_Validate(const TB_Config &c, string &err)
  {
   err = "";
   if(!TB_InList(c.mode, TB_ENUM_MODE))               { err = "invalid mode";          return false; }
   if(!TB_InList(c.entry_mode, TB_ENUM_ENTRY_MODE))   { err = "invalid entry_mode";    return false; }
   if(!TB_InList(c.htf_mode, TB_ENUM_HTF_MODE))       { err = "invalid htf_mode";      return false; }
   if(!TB_InList(c.execution_tf, TB_ENUM_EXECUTION_TF)){ err = "invalid execution_tf"; return false; }
   if(!(c.risk_percent > TB_RISK_MIN_EXCL))           { err = "risk_percent must be > 0"; return false; }
   if(!(c.max_open_risk_percent > TB_OPEN_RISK_MIN_EXCL)) { err = "max_open_risk_percent must be > 0"; return false; }
   if(c.max_open_risk_percent < c.risk_percent)       { err = "max_open_risk_percent < risk_percent"; return false; }
   if(c.max_positions < TB_MIN_POSITIONS)             { err = "max_positions too small"; return false; }
   if(!TB_InList(c.setup_tf, TB_ENUM_SETUP_TF))       { err = "invalid setup_tf";      return false; }
   if(!TB_InList(c.timeout_action, TB_ENUM_TIMEOUT_ACTION)) { err = "invalid timeout_action"; return false; }
   if(c.target_multiplier < TB_TARGET_MULT_MIN || c.target_multiplier > TB_TARGET_MULT_MAX) { err = "target_multiplier outside range"; return false; }
   if(c.scalp_window_minutes < TB_WINDOW_MIN || c.scalp_window_minutes > TB_WINDOW_MAX) { err = "scalp_window_minutes outside range"; return false; }
   if(c.max_positions_per_basket < TB_BASKET_POS_MIN || c.max_positions_per_basket > TB_BASKET_POS_MAX) { err = "max_positions_per_basket outside range"; return false; }
   if(c.max_positions_per_basket > c.max_positions)   { err = "max_positions_per_basket > max_positions"; return false; }
   if(c.min_confirmations < 0 || c.min_confirmations > c.enabled_indicator_count) { err = "min_confirmations exceeds enabled indicators"; return false; }
   if(c.max_trades_per_day < TB_MIN_TRADES_PER_DAY)   { err = "max_trades_per_day too small"; return false; }
   if(c.lockout_after_losses < TB_MIN_LOCKOUT_LOSSES) { err = "lockout_after_losses too small"; return false; }
   if(c.lockout_after_losses > c.max_trades_per_day)  { err = "lockout_after_losses > max_trades_per_day"; return false; }
   if(c.reset_minutes < TB_RESET_MINUTES_MIN || c.reset_minutes > TB_RESET_MINUTES_MAX) { err = "reset_minutes outside range"; return false; }
   if(c.reset_utc_offset_seconds < TB_OFFSET_MIN || c.reset_utc_offset_seconds > TB_OFFSET_MAX) { err = "reset offset outside range"; return false; }
   if(c.mode == "LIVE" && !c.live_confirmed)          { err = "LIVE requires live_confirmed"; return false; }
   return true;
  }

//--- the ONLY gate that may allow OrderSend. SIGNAL_ONLY never trades.
bool TB_OrdersAllowed(const TB_Config &c)
  {
   string err;
   if(!TB_Validate(c, err))
      return false;
   if(c.mode == "SIGNAL_ONLY")
      return false;
   if(c.mode == "LIVE" && !c.live_confirmed)
      return false;
   return true;
  }

//--- trading day id: integer days since 1970-01-01 in the reset timezone
long TB_TradingDayId(const long utc_epoch, const int utc_offset_sec, const int reset_minutes)
  {
   long local = utc_epoch + (long)utc_offset_sec - (long)reset_minutes * 60;
   if(local >= 0)
      return local / 86400;
   return -((-local + 86399) / 86400);
  }

//--- start of the current trading day in UTC (for loss/deal calculations)
long TB_TradingDayStartUtc(const long utc_epoch, const int utc_offset_sec, const int reset_minutes)
  {
   long day = TB_TradingDayId(utc_epoch, utc_offset_sec, reset_minutes);
   return day * 86400 - (long)utc_offset_sec + (long)reset_minutes * 60;
  }

//--- MT5 history functions (HistorySelect etc.) take TRADE SERVER time, not UTC
datetime TB_UtcToServer(const datetime utc)
  {
   return utc + (datetime)(TimeTradeServer() - TimeGMT());
  }

//+------------------------------------------------------------------+
//| Restart-safe daily counters (terminal GlobalVariables)           |
//| Reset happens only when the trading day ADVANCES. If the clock   |
//| goes backwards the state is kept and an anomaly is flagged.      |
//+------------------------------------------------------------------+
struct TB_Daily
  {
   long   day;
   int    trades;
   int    losses;
   int    consecutive_losses;
   bool   locked_out;
   bool   clock_anomaly;
  };

string TB_Key(const long magic, const string name)
  {
   return "TB_" + (string)magic + "_" + name;
  }

void TB_DailyLoad(const long magic, TB_Daily &d)
  {
   d.day                = -1;
   d.trades             = 0;
   d.losses             = 0;
   d.consecutive_losses = 0;
   d.locked_out         = false;
   d.clock_anomaly      = false;
   if(GlobalVariableCheck(TB_Key(magic, "DAY")))
     {
      d.day                = (long)GlobalVariableGet(TB_Key(magic, "DAY"));
      d.trades             = (int)GlobalVariableGet(TB_Key(magic, "TRADES"));
      d.losses             = (int)GlobalVariableGet(TB_Key(magic, "LOSSES"));
      d.consecutive_losses = (int)GlobalVariableGet(TB_Key(magic, "CONSEC"));
      d.locked_out         = GlobalVariableGet(TB_Key(magic, "LOCK")) > 0.5;
     }
  }

void TB_DailySave(const long magic, const TB_Daily &d)
  {
   GlobalVariableSet(TB_Key(magic, "DAY"),    (double)d.day);
   GlobalVariableSet(TB_Key(magic, "TRADES"), (double)d.trades);
   GlobalVariableSet(TB_Key(magic, "LOSSES"), (double)d.losses);
   GlobalVariableSet(TB_Key(magic, "CONSEC"), (double)d.consecutive_losses);
   GlobalVariableSet(TB_Key(magic, "LOCK"),   d.locked_out ? 1.0 : 0.0);
  }

//--- returns true when counters were reset (a new trading day began)
bool TB_DailyRoll(TB_Daily &d, const long today)
  {
   if(d.day == -1)
     { d.day = today; return false; }
   if(today == d.day)
     { d.clock_anomaly = false; return false; }
   if(today < d.day)
     { d.clock_anomaly = true;  return false; }   // never reset on a backwards clock
   d.day           = today;
   d.trades        = 0;
   d.losses        = 0;
   d.locked_out    = false;
   d.clock_anomaly = false;                       // consecutive_losses intentionally kept
   return true;
  }

#endif

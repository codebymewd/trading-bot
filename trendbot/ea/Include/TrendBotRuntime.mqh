//+------------------------------------------------------------------+
//| TrendBotRuntime.mqh - the EA's runtime configuration             |
//| Maps the backend's JSON to structs, validates it INDEPENDENTLY   |
//| (shared contract limits), and persists the last applied config.  |
//| Not yet compiled in MetaEditor.                                  |
//+------------------------------------------------------------------+
#property strict
#ifndef TB_RUNTIME_MQH
#define TB_RUNTIME_MQH

#include "TrendBotCore.mqh"
#include "TrendBotScalp.mqh"
#include "TrendBotJson.mqh"

struct TB_Runtime
  {
   TB_Config      core;
   TB_ScalpParams scalp;
   int            config_version;
   string         config_hash;
   int            heartbeat_seconds;
   int            stale_config_seconds;
   bool           kill_switch;
   long           allowed_logins[5];
   int            n_logins;
   string         be_method;
   double         be_trigger_r;
   int            be_offset_points;
   double         partial_close_percent;
   double         be_tolerance_money;
   int            max_consecutive_losses;      // 0 = none
   double         daily_loss_limit_percent;    // 0 = none
   long           magic;
   bool           news_low_allow;
   bool           news_medium_allow;
   bool           news_high_allow;
   string         news_on_missing;             // "block" | "allow"
   bool           vol_allow_normal;
   bool           vol_allow_elevated;
  };

double TB_JN(TB_Json &j, const string prefix, const string path, const double def = 0.0)
  {
   int x = j.Path(prefix + path);
   return (x < 0) ? def : j.Num(x, def);
  }

string TB_JS(TB_Json &j, const string prefix, const string path, const string def = "")
  {
   int x = j.Path(prefix + path);
   return (x < 0) ? def : j.Str(x, def);
  }

bool TB_JB(TB_Json &j, const string prefix, const string path, const bool def = false)
  {
   int x = j.Path(prefix + path);
   return (x < 0) ? def : j.Bool(x, def);
  }

int TB_TfSeconds(const string tf)
  {
   if(tf == "M1")  return 60;
   if(tf == "M5")  return 300;
   if(tf == "M15") return 900;
   if(tf == "M30") return 1800;
   if(tf == "H1")  return 3600;
   return 0;
  }

//--- every path the EA needs must exist: a partial config is rejected, never guessed
bool TB_RequirePaths(TB_Json &j, const string prefix, string &err)
  {
   string need[] = {"schema_version", "config_version", "config_hash", "mode", "live_confirmed", "risk.risk_percent",
                    "risk.max_open_risk_percent", "risk.max_simultaneous_positions", "scalp.target_multiplier",
                    "scalp.scalp_window_minutes", "scalp.timeout_action", "scalp.max_positions_per_basket",
                    "scalp.max_spread_to_stop_ratio", "structure.htf_mode", "structure.execution_tf", "structure.setup_tf",
                    "structure.max_entry_range_pct", "entry.entry_mode", "entry.sl_buffer_atr", "entry.min_sl_atr",
                    "entry.max_sl_atr", "entry.max_chase_r", "entry.setup_max_bars", "setup.fvg_setup_enabled",
                    "setup.sweep_setup_enabled", "setup.require_bos_linked_fvg", "setup.trigger_window_bars",
                    "setup.sweep_setup_max_age_bars", "setup.use_premium_discount", "liquidity.sweep_required",
                    "confirmation.enabled_indicators", "confirmation.minimum_confirmations", "confirmation.params.rsi_period",
                    "daily.max_trades_per_day", "daily.lockout_after_losses", "derived.reset_utc_offset_seconds",
                    "derived.reset_minutes", "safety.stale_config_seconds", "safety.heartbeat_seconds",
                    "safety.kill_switch", "broker.allowed_logins", "break_even.method", "news.high.allow_entries",
                    "volatility.allowed_states"};
   for(int i = 0; i < ArraySize(need); i++)
      if(j.Path(prefix + need[i]) < 0)
        { err = "missing " + need[i]; return false; }
   int off = j.Path(prefix + "derived.reset_utc_offset_seconds");
   if(j.t[off] != TBJ_NUMBER)
     { err = "reset timezone 'broker_server' is not supported yet (use a fixed-offset zone such as Africa/Lagos)"; return false; }
   return true;
  }

bool TB_RuntimeFromJson(TB_Json &j, const string prefix, TB_Runtime &rt, string &err)
  {
   err = "";
   if(!TB_RequirePaths(j, prefix, err)) return false;
   TB_LoadDefaults(rt.core);
   TB_DefaultScalpParams(rt.scalp);
   TB_Config c = rt.core;
   c.mode                     = TB_JS(j, prefix, "mode");
   c.live_confirmed           = TB_JB(j, prefix, "live_confirmed");
   c.risk_percent             = TB_JN(j, prefix, "risk.risk_percent");
   c.max_open_risk_percent    = TB_JN(j, prefix, "risk.max_open_risk_percent");
   c.max_positions            = (int)TB_JN(j, prefix, "risk.max_simultaneous_positions");
   c.target_multiplier        = TB_JN(j, prefix, "scalp.target_multiplier");
   c.htf_mode                 = TB_JS(j, prefix, "structure.htf_mode");
   c.entry_mode               = TB_JS(j, prefix, "entry.entry_mode");
   c.execution_tf             = TB_JS(j, prefix, "structure.execution_tf");
   c.setup_tf                 = TB_JS(j, prefix, "structure.setup_tf");
   c.scalp_window_minutes     = (int)TB_JN(j, prefix, "scalp.scalp_window_minutes");
   c.timeout_action           = TB_JS(j, prefix, "scalp.timeout_action");
   c.max_positions_per_basket = (int)TB_JN(j, prefix, "scalp.max_positions_per_basket");
   c.min_confirmations        = (int)TB_JN(j, prefix, "confirmation.minimum_confirmations");
   c.enabled_indicator_count  = j.Count(j.Path(prefix + "confirmation.enabled_indicators"));
   c.sweep_required           = TB_JB(j, prefix, "liquidity.sweep_required");
   c.reset_utc_offset_seconds = (int)TB_JN(j, prefix, "derived.reset_utc_offset_seconds");
   c.reset_minutes            = (int)TB_JN(j, prefix, "derived.reset_minutes");
   c.max_trades_per_day       = (int)TB_JN(j, prefix, "daily.max_trades_per_day");
   c.lockout_after_losses     = (int)TB_JN(j, prefix, "daily.lockout_after_losses");
   rt.core = c;

   TB_ScalpParams s = rt.scalp;
   s.gate_h4_only             = (c.htf_mode == "H4_ONLY");
   s.fvg_setup                = TB_JB(j, prefix, "setup.fvg_setup_enabled");
   s.sweep_setup              = TB_JB(j, prefix, "setup.sweep_setup_enabled");
   s.require_bos_linked_fvg   = TB_JB(j, prefix, "setup.require_bos_linked_fvg");
   s.trigger_window_bars      = (int)TB_JN(j, prefix, "setup.trigger_window_bars");
   s.sweep_setup_max_age_bars = (int)TB_JN(j, prefix, "setup.sweep_setup_max_age_bars");
   s.zone_max_age_bars        = (int)TB_JN(j, prefix, "entry.setup_max_bars");
   s.use_premium_discount     = TB_JB(j, prefix, "setup.use_premium_discount");
   s.max_entry_range_pct      = TB_JN(j, prefix, "structure.max_entry_range_pct");
   s.sl_buffer_atr            = TB_JN(j, prefix, "entry.sl_buffer_atr");
   s.min_sl_atr               = TB_JN(j, prefix, "entry.min_sl_atr");
   s.max_sl_atr               = TB_JN(j, prefix, "entry.max_sl_atr");
   s.max_chase_r              = TB_JN(j, prefix, "entry.max_chase_r");
   s.target_multiplier        = c.target_multiplier;
   s.max_spread_to_stop_ratio = TB_JN(j, prefix, "scalp.max_spread_to_stop_ratio");
   int ind = j.Path(prefix + "confirmation.enabled_indicators");
   s.enabled[0] = j.HasString(ind, "RSI");
   s.enabled[1] = j.HasString(ind, "MACD");
   s.enabled[2] = j.HasString(ind, "BB");
   s.enabled[3] = j.HasString(ind, "STOCH");
   s.enabled[4] = j.HasString(ind, "CCI");
   s.minimum_confirmations    = c.min_confirmations;
   s.setup_tf_seconds         = TB_TfSeconds(c.setup_tf);
   s.trigger_tf_seconds       = TB_TfSeconds(c.execution_tf);
   s.ind.rsi_period     = (int)TB_JN(j, prefix, "confirmation.params.rsi_period", 14);
   s.ind.rsi_midline    = TB_JN(j, prefix, "confirmation.params.rsi_midline", 50.0);
   s.ind.macd_fast      = (int)TB_JN(j, prefix, "confirmation.params.macd_fast", 12);
   s.ind.macd_slow      = (int)TB_JN(j, prefix, "confirmation.params.macd_slow", 26);
   s.ind.macd_signal    = (int)TB_JN(j, prefix, "confirmation.params.macd_signal", 9);
   s.ind.stoch_k        = (int)TB_JN(j, prefix, "confirmation.params.stoch_k", 14);
   s.ind.stoch_d        = (int)TB_JN(j, prefix, "confirmation.params.stoch_d", 3);
   s.ind.stoch_slowing  = (int)TB_JN(j, prefix, "confirmation.params.stoch_slowing", 3);
   s.ind.bb_period      = (int)TB_JN(j, prefix, "confirmation.params.bb_period", 20);
   s.ind.bb_deviation   = TB_JN(j, prefix, "confirmation.params.bb_deviation", 2.0);
   s.ind.cci_period     = (int)TB_JN(j, prefix, "confirmation.params.cci_period", 14);
   s.ind.cci_threshold  = TB_JN(j, prefix, "confirmation.params.cci_threshold", 100.0);
   rt.scalp = s;

   rt.config_version         = (int)TB_JN(j, prefix, "config_version");
   rt.config_hash            = TB_JS(j, prefix, "config_hash");
   rt.heartbeat_seconds      = (int)TB_JN(j, prefix, "safety.heartbeat_seconds");
   rt.stale_config_seconds   = (int)TB_JN(j, prefix, "safety.stale_config_seconds");
   rt.kill_switch            = TB_JB(j, prefix, "safety.kill_switch");
   rt.be_method              = TB_JS(j, prefix, "break_even.method");
   rt.be_trigger_r           = TB_JN(j, prefix, "break_even.trigger_r", 1.0);
   rt.be_offset_points       = (int)TB_JN(j, prefix, "break_even.be_offset_points");
   rt.partial_close_percent  = TB_JN(j, prefix, "break_even.partial_close_percent");
   rt.be_tolerance_money     = TB_JN(j, prefix, "daily.be_tolerance_money");
   rt.max_consecutive_losses = (int)TB_JN(j, prefix, "daily.max_consecutive_losses");
   rt.daily_loss_limit_percent = TB_JN(j, prefix, "daily.daily_loss_limit_percent");
   rt.magic                  = (long)TB_JN(j, prefix, "magic_number", 240001);
   rt.news_low_allow         = TB_JB(j, prefix, "news.low.allow_entries", true);
   rt.news_medium_allow      = TB_JB(j, prefix, "news.medium.allow_entries", true);
   rt.news_high_allow        = TB_JB(j, prefix, "news.high.allow_entries", false);
   rt.news_on_missing        = TB_JS(j, prefix, "news.on_data_unavailable", "block");
   int vs = j.Path(prefix + "volatility.allowed_states");
   rt.vol_allow_normal       = j.HasString(vs, "normal");
   rt.vol_allow_elevated     = j.HasString(vs, "elevated");
   int lg = j.Path(prefix + "broker.allowed_logins");
   rt.n_logins = 0;
   for(int i = 0; i < j.Count(lg) && i < 5; i++)
     {
      rt.allowed_logins[rt.n_logins] = (long)j.Num(j.At(lg, i));
      rt.n_logins++;
     }
   if(j.Count(lg) > 5) { err = "too many allowed logins"; return false; }
   return true;
  }

//--- independent validation (the backend also validated; the EA never trusts it blindly)
bool TB_RuntimeValidate(const TB_Runtime &rt, TB_Json &j, const string prefix, string &err)
  {
   if((int)TB_JN(j, prefix, "schema_version") != TB_SCHEMA_VERSION)
     { err = "unsupported schema_version"; return false; }
   if(!TB_Validate(rt.core, err)) return false;
   if(rt.core.mode == "LIVE")
     { err = "live_trading_not_available_in_this_version"; return false; }
   if(rt.heartbeat_seconds < 1 || rt.heartbeat_seconds > 300)
     { err = "heartbeat_seconds outside 1..300"; return false; }
   if(rt.stale_config_seconds < 30)
     { err = "stale_config_seconds below 30"; return false; }
   if(rt.scalp.setup_tf_seconds <= 0 || rt.scalp.trigger_tf_seconds <= 0 || rt.scalp.setup_tf_seconds <= rt.scalp.trigger_tf_seconds)
     { err = "setup_tf must be higher than execution_tf"; return false; }
   if(!rt.scalp.fvg_setup && !rt.scalp.sweep_setup)
     { err = "no setup type enabled"; return false; }
   if(rt.scalp.trigger_window_bars < 1 || rt.scalp.sweep_setup_max_age_bars < 1 || rt.scalp.zone_max_age_bars < 1)
     { err = "setup windows must be >= 1"; return false; }
   if(!(rt.scalp.max_sl_atr > rt.scalp.min_sl_atr))
     { err = "max_sl_atr must exceed min_sl_atr"; return false; }
   if(rt.config_version < 1 || rt.config_hash == "")
     { err = "missing version or hash"; return false; }
   if(rt.be_method != "off" && rt.be_method != "A" && rt.be_method != "B" && rt.be_method != "C")
     { err = "invalid break-even method"; return false; }
   if(rt.partial_close_percent < 0.0 || rt.partial_close_percent >= 100.0)
     { err = "invalid partial_close_percent"; return false; }
   if(rt.news_on_missing != "block" && rt.news_on_missing != "allow")
     { err = "invalid news_on_data_unavailable"; return false; }
   return true;
  }

bool TB_AccountAllowed(const TB_Runtime &rt, const long login)
  {
   for(int i = 0; i < rt.n_logins; i++)
      if(rt.allowed_logins[i] == login) return true;
   return false;
  }

//--- persistence of the last applied heartbeat response (contains the config)
string TB_ConfigFile()
  {
   return "TrendBot_config_" + IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) + ".json";
  }

void TB_SaveConfigRaw(const string raw)
  {
   int h = FileOpen(TB_ConfigFile(), FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE) { Print("TrendBot: cannot save config, err=", GetLastError()); return; }
   FileWriteString(h, raw);
   FileClose(h);
  }

string TB_LoadConfigRaw()
  {
   int h = FileOpen(TB_ConfigFile(), FILE_READ | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE) return "";
   string raw = "";
   while(!FileIsEnding(h))
      raw += FileReadString(h);
   FileClose(h);
   return raw;
  }

#endif

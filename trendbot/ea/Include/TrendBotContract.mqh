// AUTO-GENERATED from config/contract.json by tools/gen_mql5.py. DO NOT EDIT.
#property strict
#ifndef TB_CONTRACT_MQH
#define TB_CONTRACT_MQH

#define TB_SCHEMA_VERSION 3

const string TB_ENUM_MODE[] = {"SIGNAL_ONLY", "DEMO", "LIVE"};
const string TB_ENUM_ENTRY_MODE[] = {"market_confirmed"};
const string TB_ENUM_HTF_MODE[] = {"H4_AND_D1", "H4_ONLY"};
const string TB_ENUM_EXECUTION_TF[] = {"M1", "M5"};
const string TB_ENUM_BE_METHOD[] = {"off", "A", "B", "C"};
const string TB_ENUM_SETUP_TF[] = {"M15", "M30", "H1"};
const string TB_ENUM_TIMEOUT_ACTION[] = {"CLOSE_ALL", "MOVE_TO_BREAKEVEN", "KEEP_RUNNING", "CLOSE_IF_LOSS"};
const string TB_ENUM_INDICATORS[] = {"MACD", "STOCH", "BB", "RSI", "CCI"};

const double TB_TARGET_MULT_MIN = 0.5;
const double TB_TARGET_MULT_MAX = 10.0;
const int TB_WINDOW_MIN = 1;
const int TB_WINDOW_MAX = 240;
const int TB_BASKET_POS_MIN = 1;
const int TB_BASKET_POS_MAX = 10;
const int TB_RESET_MINUTES_MIN = 0;
const int TB_RESET_MINUTES_MAX = 1439;
const int TB_OFFSET_MIN = -43200;
const int TB_OFFSET_MAX = 50400;
const double TB_RISK_MIN_EXCL = 0.0;
const double TB_OPEN_RISK_MIN_EXCL = 0.0;
const int TB_MIN_POSITIONS = 1;
const int TB_MIN_TRADES_PER_DAY = 1;
const int TB_MIN_LOCKOUT_LOSSES = 1;

const string TB_DEF_MODE = "SIGNAL_ONLY";
const double TB_DEF_RISK = 1.0;
const double TB_DEF_OPEN_RISK = 1.0;
const int TB_DEF_POSITIONS = 1;
const double TB_DEF_TARGET_MULT = 3.0;
const string TB_DEF_EXEC_TF = "M5";
const string TB_DEF_SETUP_TF = "M15";
const int TB_DEF_WINDOW = 15;
const string TB_DEF_TIMEOUT = "CLOSE_ALL";
const int TB_DEF_BASKET_POS = 1;
const int TB_DEF_MIN_CONF = 3;
const int TB_DEF_ENABLED_COUNT = 4;
const string TB_DEF_HTF_MODE = "H4_AND_D1";
const string TB_DEF_ENTRY_MODE = "market_confirmed";
const bool TB_DEF_SWEEP_REQUIRED = false;
const string TB_DEF_RESET_TZ = "Africa/Lagos";
const int TB_DEF_RESET_OFFSET = 3600;
const int TB_DEF_RESET_MINUTES = 0;
const int TB_DEF_MAX_TRADES = 2;
const int TB_DEF_LOCKOUT_LOSSES = 2;

#define TB_VEC_COUNT 9
const long TB_VEC_EPOCH[] = {1792450799, 1792450800, 1792454400, 1792368000, 1792407600, 1798758000, 1835391600, 1792385999, 1792386000};
const int TB_VEC_OFFSET[] = {3600, 3600, 3600, 3600, 3600, 3600, 3600, 3600, 3600};
const int TB_VEC_RESET_MIN[] = {0, 0, 0, 0, 0, 0, 0, 360, 360};
const long TB_VEC_DAY[] = {20745, 20746, 20746, 20745, 20745, 20819, 21243, 20744, 20745};

#define TB_INVALID_COUNT 18
const string TB_INVALID_FIELD[] = {"target_multiplier", "target_multiplier", "target_multiplier", "scalp_window_minutes", "scalp_window_minutes", "timeout_action", "max_positions_per_basket", "minimum_confirmations", "risk_percent", "risk_percent", "max_simultaneous_positions", "entry_mode", "entry_mode", "htf_mode", "execution_tf", "setup_tf", "mode", "max_trades_per_day"};
const string TB_INVALID_VALUE[] = {"0.4", "10.1", "0", "0", "241", "HOLD_FOREVER", "0", "9", "0", "-1", "0", "market_anywhere", "fvg_50", "H1_ONLY", "H1", "M5", "PAPER", "0"};

#endif

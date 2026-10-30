//+------------------------------------------------------------------+
//| TrendBotTrade.mqh - actual MQL5 order execution for TrendBotEA   |
//| The EA is the FINAL execution authority: every order re-passes   |
//| safety checks broker-side (config fresh, account identity, demo/ |
//| live match, kill switch, paused, spread, stops level, volume     |
//| rules, margin) before OrderSend. Lots are computed with the same |
//| contract as the Python sizing module and rounded DOWN to the     |
//| broker volume step; risk is never silently increased.            |
//| Duplicate protection: one order per setup key, remembered in a  |
//| state file that survives EA restarts.                            |
//+------------------------------------------------------------------+
#property strict
#ifndef TB_TRADE_MQH
#define TB_TRADE_MQH

#include "TrendBotRuntime.mqh"

//--- broker snapshot used by the safety gate (mirrors executor.BrokerAccount/spec/quote)
struct TB_BrokerAcct
  {
   long   login;
   double balance;
   double equity;
   double free_margin;
   bool   is_demo;
   string currency;
  };

struct TB_SymbolSpec
  {
   bool   ok;
   double point;
   int    digits;
   double tick_size;
   double tick_value_profit;   // account currency per tick per lot (profit direction)
   double tick_value_loss;     // account currency per tick per lot (loss direction)
   double volume_min;
   double volume_max;
   double volume_step;
   long   stops_level_points;
   double commission_per_lot;  // round turn, account currency
   double spread;              // current spread in price units
  };

void TB_ReadBroker(TB_BrokerAcct &a)
  {
   a.login       = AccountInfoInteger(ACCOUNT_LOGIN);
   a.balance     = AccountInfoDouble(ACCOUNT_BALANCE);
   a.equity      = AccountInfoDouble(ACCOUNT_EQUITY);
   a.free_margin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   a.is_demo     = (AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO);
   a.currency    = AccountInfoString(ACCOUNT_CURRENCY);
  }

bool TB_ReadSpec(const string sym, TB_SymbolSpec &s)
  {
   s.ok = false;
   if(!SymbolSelect(sym, true)) return false;
   s.point              = SymbolInfoDouble(sym, SYMBOL_POINT);
   s.digits             = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   s.tick_size          = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE);
   s.tick_value_profit  = SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE);
   s.tick_value_loss    = s.tick_value_profit;   // hedging/netting: same magnitude per tick
   s.volume_min         = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   s.volume_max         = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   s.volume_step        = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   s.stops_level_points = SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL);
   s.commission_per_lot = 0.0;                   // filled best-effort below from the symbol commission
   double comm = 0.0;
   if(SymbolInfoDouble(sym, SYMBOL_COMMISSION_PER_LOT_SIDE, comm))
      s.commission_per_lot = comm * 2.0;         // both sides ~ round turn approximation
   double bid = 0.0, ask = 0.0;
   if(!SymbolInfoDouble(sym, SYMBOL_BID, bid) || !SymbolInfoDouble(sym, SYMBOL_ASK, ask)) return false;
   if(bid <= 0.0 || ask < bid || s.point <= 0.0 || s.tick_size <= 0.0 ||
      s.volume_min <= 0.0 || s.volume_step <= 0.0) return false;
   s.spread = ask - bid;
   s.ok = true;
   return true;
  }

double TB_QuotePrice(const string sym, const int dir)   // entry price for a market order
  {
   double p = 0.0;
   if(dir == 1) { if(!SymbolInfoDouble(sym, SYMBOL_ASK, p)) return 0.0; }
   else         { if(!SymbolInfoDouble(sym, SYMBOL_BID, p)) return 0.0; }
   return p;
  }

//--- money lost per lot when price moves 'risk_price' against the position (account currency)
double TB_LossPerLot(const TB_SymbolSpec &s, const double risk_price)
  {
   if(s.tick_size <= 0.0) return 0.0;
   return (risk_price / s.tick_size) * s.tick_value_loss + s.commission_per_lot;
  }

//--- round DOWN to the broker grid; returns 0.0 when no legal volume fits the budget
double TB_NormalizeLots(const TB_SymbolSpec &s, const double want)
  {
   if(want <= 0.0 || s.volume_step <= 0.0) return 0.0;
   double steps = MathFloor(want / s.volume_step + 1e-9);
   double lots  = steps * s.volume_step;
   lots = MathRound(lots / s.volume_step) * s.volume_step;    // kill binary drift
   if(lots < s.volume_min - 1e-12) return 0.0;                // min lot must fit or NO trade
   if(lots > s.volume_max) lots = s.volume_max;
   return lots;
  }

//--- exact duplicate of Python executor.position_risk_money semantics
double TB_PositionRiskMoney(const TB_SymbolSpec &s, const double entry, const double sl, const double lots)
  {
   double loss = TB_LossPerLot(s, MathAbs(entry - sl));
   return loss * lots;
  }

//--- margin check exactly like OrderCalcMargin (dir: 1 buy, -1 sell)
bool TB_MarginOk(const string sym, const int dir, const double lots, const double price, const double need_free)
  {
   double margin = 0.0;
   ENUM_ORDER_TYPE ot = (dir == 1) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(!OrderCalcMargin(ot, sym, lots, price, margin)) return false;
   return margin <= need_free;
  }

double TB_RoundPrice(const double v, const int digits)
  {
   double m = MathPow(10.0, digits);
   return MathRound(v * m) / m;
  }

//--- final pre-trade safety gate inside the EA. Returns "" when allowed, else the reason.
string TB_TradeAllowed(const TB_Runtime &rt, const TB_BrokerAcct &a, const TB_SymbolSpec &s,
                       const bool config_fresh, const bool paused, const int dir,
                       const double entry, const double sl, const double tp)
  {
   if(rt.kill_switch)                          return "kill_switch";
   if(paused)                                  return "paused";
   if(!config_fresh)                           return "stale_config";
   if(rt.core.mode == "SIGNAL_ONLY")           return "orders_not_permitted";
   if(!TB_AccountAllowed(rt, a.login))         return "account_not_allowed";
   if(rt.core.mode == "LIVE")                  return "live_trading_not_available";   // locked until explicitly validated
   if(rt.core.mode == "DEMO" && !a.is_demo)    return "demo_mode_on_live_account";
   if(!s.ok)                                   return "symbol_spec_unavailable";
   if(s.spread > rt.scalp.max_spread_to_stop_ratio * MathAbs(entry - sl))
                                               return "spread_too_wide";             // broker-side spread check
   double stop_dist = MathAbs(entry - sl) / s.point;
   if(stop_dist < (double)s.stops_level_points) return "stop_distance_too_small";
   double tp_dist = MathAbs(tp - entry) / s.point;
   if(tp_dist < (double)s.stops_level_points)   return "tp_distance_too_small";
   if(dir == 1 && !(sl < entry && tp > entry))  return "invalid_sl_tp_buy";
   if(dir == -1 && !(sl > entry && tp < entry)) return "invalid_sl_tp_sell";
   return "";
  }

//--- send one market order. Returns ticket (>0) and fills result fields; retcode mapped to a reason string.
long TB_SendMarket(const string sym, const int dir, const double lots, double sl, double tp,
                   const string comment, const int deviation_points, const long magic,
                   const int digits, double &fill_price, string &reason)
  {
   fill_price = 0.0;
   reason = "";
   sl = TB_RoundPrice(sl, digits);
   tp = TB_RoundPrice(tp, digits);
   MqlTradeRequest req;
   MqlTradeResult  res;
   ZeroMemory(req);
   ZeroMemory(res);
   req.action       = TRADE_ACTION_DEAL;
   req.symbol       = sym;
   req.volume       = lots;
   req.type         = (dir == 1) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.price        = TB_QuotePrice(sym, dir);
   req.sl           = sl;
   req.tp           = tp;
   req.deviation    = deviation_points;
   req.magic        = magic;
   req.comment      = comment;
   req.type_filling = ORDER_FILLING_FOK;
   if(req.price <= 0.0) { reason = "no_quote"; return -1; }
   if(!OrderSend(req, res))
     {
      reason = "send_failed_retc" + IntegerToString((long)res.retcode);
      return -1;
     }
   if(res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_DONE_PARTIAL &&
      res.retcode != TRADE_RETCODE_PLACED)
     {
      reason = "rejected_retc" + IntegerToString((long)res.retcode);
      return -1;
     }
   fill_price = res.price;
   if(fill_price <= 0.0) fill_price = req.price;
   long ticket = (long)res.deal;
   if(ticket <= 0) ticket = (long)res.order;
   if(ticket <= 0)
     {
      // some brokers return only the position; find it by magic+symbol right after the fill
      for(int p = PositionsTotal() - 1; p >= 0; p--)
        {
         ulong tk = PositionGetTicket(p);
         if(tk == 0) continue;
         if(PositionGetString(POSITION_SYMBOL) == sym && PositionGetInteger(POSITION_MAGIC) == magic)
           { ticket = (long)tk; break; }
        }
     }
   if(ticket <= 0) { reason = "no_ticket_returned"; return -1; }
   return ticket;
  }

//--- close a position by ticket (market opposite). Used by dashboard commands and basket management.
bool TB_CloseTicket(const long ticket, const long magic, string &reason)
  {
   reason = "";
   if(!PositionSelectByTicket((ulong)ticket))
     { reason = "position_not_found"; return false; }
   if(PositionGetInteger(POSITION_MAGIC) != magic)
     { reason = "foreign_position"; return false; }          // never touch trades we did not open
   string sym = PositionGetString(POSITION_SYMBOL);
   int dir    = (PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY) ? -1 : 1;  // close = opposite
   double lots = PositionGetDouble(POSITION_VOLUME);
   MqlTradeRequest req;
   MqlTradeResult  res;
   ZeroMemory(req);
   ZeroMemory(res);
   req.action       = TRADE_ACTION_DEAL;
   req.position     = (ulong)ticket;
   req.symbol       = sym;
   req.volume       = lots;
   req.type         = (dir == 1) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.price        = TB_QuotePrice(sym, dir);
   req.deviation    = 20;
   req.magic        = magic;
   req.comment      = "TB-close";
   req.type_filling = ORDER_FILLING_FOK;
   if(req.price <= 0.0) { reason = "no_quote"; return false; }
   if(!OrderSend(req, res) || (res.retcode != TRADE_RETCODE_DONE && res.retcode != TRADE_RETCODE_DONE_PARTIAL))
     { reason = "close_failed_retc" + IntegerToString((long)res.retcode); return false; }
   return true;
  }

//--- move a stop-loss (break-even / trailing). Broker-side validation applies.
bool TB_ModifySL(const long ticket, const long magic, const double new_sl, const int digits, string &reason)
  {
   reason = "";
   if(!PositionSelectByTicket((ulong)ticket))
     { reason = "position_not_found"; return false; }
   if(PositionGetInteger(POSITION_MAGIC) != magic)
     { reason = "foreign_position"; return false; }
   MqlTradeRequest req;
   MqlTradeResult  res;
   ZeroMemory(req);
   ZeroMemory(res);
   req.action   = TRADE_ACTION_SLTP;
   req.position = (ulong)ticket;
   req.symbol   = PositionGetString(POSITION_SYMBOL);
   req.sl       = TB_RoundPrice(new_sl, digits);
   req.tp       = PositionGetDouble(POSITION_TP);
   if(!OrderSend(req, res) || res.retcode != TRADE_RETCODE_DONE)
     { reason = "modify_failed_retc" + IntegerToString((long)res.retcode); return false; }
   return true;
  }

#endif

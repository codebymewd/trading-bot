//+------------------------------------------------------------------+
//| ExportSymbolSpec.mq5 - writes the chart symbol's trading specs   |
//| to MQL5\Files\TrendBot_<SYMBOL>_spec.csv for tools/plan_basket.py|
//| Read-only: no trading. Not yet compiled in MetaEditor.           |
//+------------------------------------------------------------------+
#property strict
#property script_show_inputs

input double InpCommissionPerLot = 0.0;   // round-turn commission per 1.0 lot in account currency (not available from MT5)

void Put(const int h, const string key, const string value)
  {
   FileWrite(h, key, value);
  }

void OnStart()
  {
   string s = _Symbol;
   if(!SymbolSelect(s, true))
      Print("ExportSymbolSpec: could not select ", s);
   double ask = SymbolInfoDouble(s, SYMBOL_ASK);
   double margin = 0.0;
   if(ask <= 0.0 || !OrderCalcMargin(ORDER_TYPE_BUY, s, 1.0, ask, margin))
      margin = 0.0;
   long mode = SymbolInfoInteger(s, SYMBOL_TRADE_MODE);
   int h = FileOpen("TrendBot_" + s + "_spec.csv", FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(h == INVALID_HANDLE)
     {
      Print("ExportSymbolSpec: cannot write file, err=", GetLastError());
      return;
     }
   Put(h, "symbol", s);
   Put(h, "digits", IntegerToString((int)SymbolInfoInteger(s, SYMBOL_DIGITS)));
   Put(h, "point", DoubleToString(SymbolInfoDouble(s, SYMBOL_POINT), 10));
   Put(h, "tick_size", DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_SIZE), 10));
   Put(h, "tick_value_profit", DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_VALUE_PROFIT), 10));
   Put(h, "tick_value_loss", DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_VALUE_LOSS), 10));
   Put(h, "volume_min", DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_MIN), 8));
   Put(h, "volume_max", DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_MAX), 8));
   Put(h, "volume_step", DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_STEP), 8));
   Put(h, "stops_level_points", IntegerToString((int)SymbolInfoInteger(s, SYMBOL_TRADE_STOPS_LEVEL)));
   Put(h, "freeze_level_points", IntegerToString((int)SymbolInfoInteger(s, SYMBOL_TRADE_FREEZE_LEVEL)));
   Put(h, "contract_size", DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_CONTRACT_SIZE), 8));
   Put(h, "margin_per_lot", DoubleToString(margin, 4));
   Put(h, "commission_per_lot", DoubleToString(InpCommissionPerLot, 4));
   Put(h, "trade_allowed", (mode == SYMBOL_TRADE_MODE_FULL) ? "1" : "0");
   Put(h, "spread_points", IntegerToString((int)SymbolInfoInteger(s, SYMBOL_SPREAD)));
   Put(h, "account_currency", AccountInfoString(ACCOUNT_CURRENCY));
   Put(h, "equity", DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2));
   Put(h, "free_margin", DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2));
   FileClose(h);
   Print("ExportSymbolSpec: wrote TrendBot_", s, "_spec.csv  (margin per lot ", DoubleToString(margin, 2), ")");
  }

//+------------------------------------------------------------------+
//| ExportBars.mq5 - exports closed D1/H4/M15/M5 bars of the chart   |
//| symbol to MQL5\Files for tools/run_scalp_reference.py.           |
//| Read-only: no trading. Not yet compiled in MetaEditor.           |
//+------------------------------------------------------------------+
#property strict
#property script_show_inputs

input int InpD1Bars  = 400;
input int InpH4Bars  = 2000;
input int InpM15Bars = 6000;
input int InpM5Bars  = 18000;

bool ExportTf(const ENUM_TIMEFRAMES tf, const int count)
  {
   MqlRates r[];
   ArraySetAsSeries(r, false);                       // index 0 = oldest
   int got = -1;
   for(int a = 0; a < 10; a++)
     {
      got = CopyRates(_Symbol, tf, 1, count, r);     // start at shift 1: closed bars only
      if(got > 0) break;
      Sleep(500);                                    // give MT5 time to download history
     }
   if(got <= 0)
     {
      Print("ExportBars: no data for ", EnumToString(tf), " err=", GetLastError());
      return false;
     }
   string fn = "TrendBot_" + _Symbol + "_" + EnumToString(tf) + "_bars.csv";
   int h = FileOpen(fn, FILE_WRITE | FILE_CSV | FILE_ANSI, ',');
   if(h == INVALID_HANDLE)
     {
      Print("ExportBars: cannot write ", fn, " err=", GetLastError());
      return false;
     }
   FileWrite(h, "#digits=" + IntegerToString(_Digits));
   FileWrite(h, "time", "open", "high", "low", "close");
   for(int i = 0; i < got; i++)
      FileWrite(h, (string)(long)r[i].time, DoubleToString(r[i].open, _Digits), DoubleToString(r[i].high, _Digits),
                DoubleToString(r[i].low, _Digits), DoubleToString(r[i].close, _Digits));
   FileClose(h);
   Print("ExportBars: ", fn, "  bars=", got, "  (requested ", count, ")");
   return true;
  }

void OnStart()
  {
   bool ok = ExportTf(PERIOD_D1, InpD1Bars);
   ok = ExportTf(PERIOD_H4, InpH4Bars) && ok;
   ok = ExportTf(PERIOD_M15, InpM15Bars) && ok;
   ok = ExportTf(PERIOD_M5, InpM5Bars) && ok;
   Print(ok ? "ExportBars: done. Files are in MQL5\\Files" : "ExportBars: finished with errors, see messages above");
  }

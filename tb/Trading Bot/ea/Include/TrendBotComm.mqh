//+------------------------------------------------------------------+
//| TrendBotComm.mqh - signed backend calls (same recipe as          |
//| backend/app/protocol.py), event queue, symbol reporting.         |
//| No trading functions in this file. Not yet compiled; verify with |
//| Scripts/TestTrendBotComm.mq5.                                    |
//+------------------------------------------------------------------+
#property strict
#ifndef TB_COMM_MQH
#define TB_COMM_MQH

#include "TrendBotJson.mqh"

int    g_tb_nonce_counter = 0;
string g_tb_events[];
int    g_tb_nevents = 0;
int    g_tb_event_counter = 0;

//--- bytes / hashing -------------------------------------------------
void TB_ToBytes(const string text, uchar &out[])
  {
   char tmp[];
   int n = StringToCharArray(text, tmp, 0, WHOLE_ARRAY, CP_UTF8);
   if(n <= 0) { ArrayResize(out, 0); return; }
   ArrayResize(out, n - 1);                       // drop the terminating zero
   for(int i = 0; i < n - 1; i++)
      out[i] = (uchar)tmp[i];
  }

bool TB_Sha256(const uchar &data[], uchar &out[])
  {
   uchar key[];
   ArrayResize(key, 0);
   ArrayResize(out, 0);
   return CryptEncode(CRYPT_HASH_SHA256, data, key, out) > 0;
  }

string TB_Hex(const uchar &a[])
  {
   string r = "";
   for(int i = 0; i < ArraySize(a); i++)
      r += StringFormat("%02x", a[i]);
   return r;
  }

string TB_Sha256Hex(const string text)
  {
   uchar b[], h[];
   TB_ToBytes(text, b);
   TB_Sha256(b, h);
   return TB_Hex(h);
  }

void TB_HmacSha256(const uchar &key[], const uchar &msg[], uchar &out[])
  {
   const int B = 64;
   uchar k[];
   int kl = ArraySize(key);
   if(kl > B)
     {
      TB_Sha256(key, k);
      kl = ArraySize(k);
     }
   else
     {
      ArrayResize(k, kl);
      for(int i = 0; i < kl; i++) k[i] = key[i];
     }
   ArrayResize(k, B);
   for(int i = kl; i < B; i++) k[i] = 0;
   uchar inner[], outer[], ih[];
   int ml = ArraySize(msg);
   ArrayResize(inner, B + ml);
   for(int i = 0; i < B; i++) inner[i] = (uchar)(k[i] ^ 0x36);
   for(int i = 0; i < ml; i++) inner[B + i] = msg[i];
   TB_Sha256(inner, ih);
   int il = ArraySize(ih);
   ArrayResize(outer, B + il);
   for(int i = 0; i < B; i++) outer[i] = (uchar)(k[i] ^ 0x5c);
   for(int i = 0; i < il; i++) outer[B + i] = ih[i];
   TB_Sha256(outer, out);
  }

string TB_HmacHex(const string secret, const string message)
  {
   uchar key[], msg[], out[];
   TB_ToBytes(secret, key);
   TB_ToBytes(message, msg);
   TB_HmacSha256(key, msg, out);
   return TB_Hex(out);
  }

string TB_SignMessage(const string method, const string path, const string ts, const string nonce, const string body)
  {
   return method + "\n" + path + "\n" + ts + "\n" + nonce + "\n" + TB_Sha256Hex(body);
  }

string TB_Nonce()
  {
   g_tb_nonce_counter++;
   return StringFormat("%I64x%I64x%x%x", (long)TimeGMT(), (long)GetTickCount64(), MathRand(), g_tb_nonce_counter);
  }

string TB_Esc(const string v)
  {
   string r = v;
   StringReplace(r, "\\", "\\\\");
   StringReplace(r, "\"", "\\\"");
   return r;
  }

//--- HTTP -------------------------------------------------------------
//--- returns the HTTP status code, or -1 on a transport error (err explains)
int TB_Post(const string base, const string path, const string body, const string secret, string &response, string &err)
  {
   response = "";
   err = "";
   string ts = IntegerToString((long)TimeGMT());           // backend checks +/- 60 s of UTC
   string nonce = TB_Nonce();
   string sig = TB_HmacHex(secret, TB_SignMessage("POST", path, ts, nonce, body));
   string headers = "Content-Type: application/json\r\nX-TB-Timestamp: " + ts + "\r\nX-TB-Nonce: " + nonce +
                    "\r\nX-TB-Signature: " + sig + "\r\n";
   char data[];
   int n = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   ArrayResize(data, (n > 0) ? n - 1 : 0);
   char result[];
   string rh;
   ResetLastError();
   int code = WebRequest("POST", base + path, headers, 5000, data, result, rh);
   if(code == -1)
     {
      int e = GetLastError();
      err = "WebRequest error " + IntegerToString(e);
      if(e == 4014) err += " (add the backend URL under Tools > Options > Expert Advisors > Allow WebRequest)";
      return -1;
     }
   uchar rb[];
   ArrayResize(rb, ArraySize(result));
   for(int i = 0; i < ArraySize(result); i++) rb[i] = (uchar)result[i];
   response = CharArrayToString(rb, 0, WHOLE_ARRAY, CP_UTF8);
   return code;
  }

//--- events --------------------------------------------------------------
//--- extra = JSON members without braces, e.g. "\"ok\":true,\"detail\":\"x\""
void TB_QueueEvent(const string type, const string extra = "")
  {
   g_tb_event_counter++;
   string id = IntegerToString((long)AccountInfoInteger(ACCOUNT_LOGIN)) + "-" + IntegerToString((long)TimeGMT()) + "-" +
               IntegerToString(g_tb_event_counter);
   string e = "{\"event_id\":\"" + id + "\",\"type\":\"" + type + "\"";
   if(extra != "") e += "," + extra;
   e += "}";
   if(ArraySize(g_tb_events) < g_tb_nevents + 1)
      ArrayResize(g_tb_events, g_tb_nevents + 32);
   if(g_tb_nevents >= 500)                           // bounded queue: drop the oldest when the backend is unreachable
     {
      for(int i = 1; i < g_tb_nevents; i++) g_tb_events[i - 1] = g_tb_events[i];
      g_tb_nevents--;
     }
   g_tb_events[g_tb_nevents] = e;
   g_tb_nevents++;
  }

//--- sends up to 20 queued events; they are removed only after an HTTP 200
bool TB_FlushEvents(const string base, const string secret, string &err)
  {
   if(g_tb_nevents == 0) return true;
   int take = MathMin(20, g_tb_nevents);
   string body = "{\"events\":[";
   for(int i = 0; i < take; i++)
     {
      if(i > 0) body += ",";
      body += g_tb_events[i];
     }
   body += "]}";
   string resp;
   int code = TB_Post(base, "/ea/events", body, secret, resp, err);
   if(code != 200)
     {
      if(code != -1) err = "events HTTP " + IntegerToString(code);
      return false;
     }
   for(int i = take; i < g_tb_nevents; i++) g_tb_events[i - take] = g_tb_events[i];
   g_tb_nevents -= take;
   return true;
  }

//--- symbol discovery ----------------------------------------------------------
string TB_AssetClass(const string name)
  {
   string up = name;
   StringToUpper(up);
   string letters = "";
   for(int i = 0; i < StringLen(up); i++)
     {
      ushort c = StringGetCharacter(up, i);
      if(c >= 'A' && c <= 'Z') letters += ShortToString(c);
     }
   if(StringLen(letters) < 3) return "other";
   string head3 = StringSubstr(letters, 0, 3);
   string forex = "USD,EUR,GBP,JPY,CHF,AUD,NZD,CAD,CNH,SEK,NOK,MXN,ZAR,TRY,SGD,HKD,PLN";
   string metals = "XAU,XAG,XPT,XPD";
   string crypto = "BTC,ETH,LTC,XRP,BCH,SOL,ADA,DOT,DOGE,BNB";
   if(StringLen(letters) >= 6 && StringFind(forex, head3) >= 0 && StringFind(forex, StringSubstr(letters, 3, 3)) >= 0)
      return "forex";
   if(StringFind(metals, head3) >= 0) return "metals";
   if(StringFind(crypto, head3) >= 0) return "crypto";
   return "other";
  }

bool TB_SymbolSpecOk(const string name)
  {
   if(SymbolInfoDouble(name, SYMBOL_POINT) <= 0.0) return false;
   if(SymbolInfoDouble(name, SYMBOL_TRADE_TICK_SIZE) <= 0.0) return false;
   if(SymbolInfoDouble(name, SYMBOL_TRADE_TICK_VALUE_PROFIT) <= 0.0) return false;
   if(SymbolInfoDouble(name, SYMBOL_TRADE_TICK_VALUE_LOSS) <= 0.0) return false;
   double vmin = SymbolInfoDouble(name, SYMBOL_VOLUME_MIN);
   double vstep = SymbolInfoDouble(name, SYMBOL_VOLUME_STEP);
   double vmax = SymbolInfoDouble(name, SYMBOL_VOLUME_MAX);
   if(vmin <= 0.0 || vstep <= 0.0 || vmax < vmin) return false;
   if(SymbolInfoInteger(name, SYMBOL_TRADE_MODE) == SYMBOL_TRADE_MODE_DISABLED) return false;
   return true;
  }

//--- JSON array of the symbols the terminal offers (Market Watch only, or all symbols)
string TB_SymbolsJson(const bool all, const int maxCount)
  {
   int total = SymbolsTotal(!all);
   string r = "[";
   int added = 0;
   for(int i = 0; i < total && added < maxCount; i++)
     {
      string nm = SymbolName(i, !all);
      if(nm == "") continue;
      if(added > 0) r += ",";
      r += "{\"name\":\"" + TB_Esc(nm) + "\",\"asset_class\":\"" + TB_AssetClass(nm) + "\",\"spec_ok\":" +
           (TB_SymbolSpecOk(nm) ? "true" : "false") + "}";
      added++;
     }
   return r + "]";
  }

#endif

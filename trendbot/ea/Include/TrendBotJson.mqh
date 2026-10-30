//+------------------------------------------------------------------+
//| TrendBotJson.mqh - small JSON parser for the backend responses   |
//| Tree stored in arrays (first-child / next-sibling).              |
//| Not yet compiled in MetaEditor: run Scripts/TestTrendBotComm.mq5 |
//+------------------------------------------------------------------+
#property strict
#ifndef TB_JSON_MQH
#define TB_JSON_MQH

#define TBJ_NULL   0
#define TBJ_BOOL   1
#define TBJ_NUMBER 2
#define TBJ_STRING 3
#define TBJ_ARRAY  4
#define TBJ_OBJECT 5

class TB_Json
  {
public:
   int      n;
   int      t[];      // node type
   string   k[];      // key (for object members)
   string   sv[];     // string value (also holds true/false text for bools)
   double   nv[];     // number value (1/0 for bools)
   int      fc[];     // first child
   int      lc[];     // last child
   int      nx[];     // next sibling
   string   s;
   int      pos;
   int      len;
   bool     ok;
   string   err;

   int NewNode(const int type, const string key)
     {
      if(ArraySize(t) < n + 1)
        {
         int cap = n + 64;
         ArrayResize(t, cap);  ArrayResize(k, cap);  ArrayResize(sv, cap);
         ArrayResize(nv, cap); ArrayResize(fc, cap); ArrayResize(lc, cap); ArrayResize(nx, cap);
        }
      t[n] = type; k[n] = key; sv[n] = ""; nv[n] = 0.0; fc[n] = -1; lc[n] = -1; nx[n] = -1;
      n++;
      return n - 1;
     }

   void Append(const int parent, const int child)
     {
      if(fc[parent] < 0) fc[parent] = child;
      else               nx[lc[parent]] = child;
      lc[parent] = child;
     }

   void SkipWs()
     {
      while(pos < len)
        {
         ushort c = StringGetCharacter(s, pos);
         if(c == ' ' || c == '\t' || c == '\r' || c == '\n') pos++;
         else break;
        }
     }

   bool Fail(const string why)
     {
      if(ok) { ok = false; err = why + " at " + IntegerToString(pos); }
      return false;
     }

   //--- parses a string literal starting at the opening quote; result in out
   bool ParseString(string &out)
     {
      out = "";
      if(pos >= len || StringGetCharacter(s, pos) != '"') return Fail("expected quote");
      pos++;
      while(pos < len)
        {
         ushort c = StringGetCharacter(s, pos);
         if(c == '"') { pos++; return true; }
         if(c == '\\')
           {
            pos++;
            if(pos >= len) return Fail("bad escape");
            ushort e = StringGetCharacter(s, pos);
            if(e == 'n') out += "\n";
            else if(e == 't') out += "\t";
            else if(e == 'r') out += "\r";
            else if(e == 'b') out += ShortToString(8);
            else if(e == 'f') out += ShortToString(12);
            else if(e == 'u')
              {
               if(pos + 4 >= len) return Fail("bad unicode escape");
               string hex = StringSubstr(s, pos + 1, 4);
               int code = 0;
               for(int i = 0; i < 4; i++)
                 {
                  ushort h = StringGetCharacter(hex, i);
                  int d = 0;
                  if(h >= '0' && h <= '9') d = h - '0';
                  else if(h >= 'a' && h <= 'f') d = 10 + h - 'a';
                  else if(h >= 'A' && h <= 'F') d = 10 + h - 'A';
                  else return Fail("bad unicode digit");
                  code = code * 16 + d;
                 }
               out += ShortToString((ushort)code);
               pos += 4;
              }
            else out += ShortToString(e);       // \" \\ \/
            pos++;
            continue;
           }
         out += ShortToString(c);
         pos++;
        }
      return Fail("unterminated string");
     }

   int ParseValue(const string key)
     {
      SkipWs();
      if(pos >= len) { Fail("unexpected end"); return -1; }
      ushort c = StringGetCharacter(s, pos);
      if(c == '{')
        {
         int node = NewNode(TBJ_OBJECT, key);
         pos++;
         SkipWs();
         if(pos < len && StringGetCharacter(s, pos) == '}') { pos++; return node; }
         while(ok)
           {
            SkipWs();
            string name;
            if(!ParseString(name)) return -1;
            SkipWs();
            if(pos >= len || StringGetCharacter(s, pos) != ':') { Fail("expected colon"); return -1; }
            pos++;
            int child = ParseValue(name);
            if(child < 0) return -1;
            Append(node, child);
            SkipWs();
            if(pos >= len) { Fail("unterminated object"); return -1; }
            ushort d = StringGetCharacter(s, pos);
            pos++;
            if(d == ',') continue;
            if(d == '}') return node;
            Fail("expected comma or brace");
            return -1;
           }
         return -1;
        }
      if(c == '[')
        {
         int node = NewNode(TBJ_ARRAY, key);
         pos++;
         SkipWs();
         if(pos < len && StringGetCharacter(s, pos) == ']') { pos++; return node; }
         while(ok)
           {
            int child = ParseValue("");
            if(child < 0) return -1;
            Append(node, child);
            SkipWs();
            if(pos >= len) { Fail("unterminated array"); return -1; }
            ushort d = StringGetCharacter(s, pos);
            pos++;
            if(d == ',') continue;
            if(d == ']') return node;
            Fail("expected comma or bracket");
            return -1;
           }
         return -1;
        }
      if(c == '"')
        {
         string v;
         if(!ParseString(v)) return -1;
         int node = NewNode(TBJ_STRING, key);
         sv[node] = v;
         return node;
        }
      if(StringSubstr(s, pos, 4) == "true")  { pos += 4; int b = NewNode(TBJ_BOOL, key); nv[b] = 1.0; sv[b] = "true";  return b; }
      if(StringSubstr(s, pos, 5) == "false") { pos += 5; int b = NewNode(TBJ_BOOL, key); nv[b] = 0.0; sv[b] = "false"; return b; }
      if(StringSubstr(s, pos, 4) == "null")  { pos += 4; return NewNode(TBJ_NULL, key); }
      int start = pos;
      while(pos < len)
        {
         ushort d = StringGetCharacter(s, pos);
         if((d >= '0' && d <= '9') || d == '-' || d == '+' || d == '.' || d == 'e' || d == 'E') pos++;
         else break;
        }
      if(pos == start) { Fail("unexpected character"); return -1; }
      int num = NewNode(TBJ_NUMBER, key);
      sv[num] = StringSubstr(s, start, pos - start);
      nv[num] = StringToDouble(sv[num]);
      return num;
     }

   bool Parse(const string text)
     {
      n = 0;
      ArrayResize(t, 0); ArrayResize(k, 0); ArrayResize(sv, 0); ArrayResize(nv, 0);
      ArrayResize(fc, 0); ArrayResize(lc, 0); ArrayResize(nx, 0);
      s = text; pos = 0; len = StringLen(text); ok = true; err = "";
      int root = ParseValue("");
      if(root < 0 || !ok) return false;
      SkipWs();
      if(pos != len) { Fail("trailing characters"); return false; }
      return true;
     }

   //--- navigation (node index 0 is the root)
   int Member(const int node, const string key)
     {
      if(node < 0 || node >= n || t[node] != TBJ_OBJECT) return -1;
      for(int c = fc[node]; c >= 0; c = nx[c])
         if(k[c] == key) return c;
      return -1;
     }

   //--- "a.b.c" path from the root
   int Path(const string path)
     {
      if(n == 0) return -1;
      int node = 0;
      string rest = path;
      while(rest != "" && node >= 0)
        {
         int dot = StringFind(rest, ".");
         string part = (dot < 0) ? rest : StringSubstr(rest, 0, dot);
         rest = (dot < 0) ? "" : StringSubstr(rest, dot + 1);
         node = Member(node, part);
        }
      return node;
     }

   int Count(const int node)
     {
      if(node < 0) return 0;
      int c = 0;
      for(int x = fc[node]; x >= 0; x = nx[x]) c++;
      return c;
     }

   int At(const int node, const int index)
     {
      if(node < 0) return -1;
      int i = 0;
      for(int x = fc[node]; x >= 0; x = nx[x])
        {
         if(i == index) return x;
         i++;
        }
      return -1;
     }

   string Str(const int node, const string def = "")
     {
      if(node < 0 || (t[node] != TBJ_STRING && t[node] != TBJ_NUMBER && t[node] != TBJ_BOOL)) return def;
      return sv[node];
     }

   double Num(const int node, const double def = 0.0)
     {
      if(node < 0 || (t[node] != TBJ_NUMBER && t[node] != TBJ_BOOL)) return def;
      return nv[node];
     }

   bool Bool(const int node, const bool def = false)
     {
      if(node < 0 || t[node] != TBJ_BOOL) return def;
      return nv[node] != 0.0;
     }

   bool HasString(const int arrayNode, const string value)
     {
      if(arrayNode < 0 || t[arrayNode] != TBJ_ARRAY) return false;
      for(int x = fc[arrayNode]; x >= 0; x = nx[x])
         if(t[x] == TBJ_STRING && sv[x] == value) return true;
      return false;
     }
  };

#endif

# u9-l1 基础工具库：util、uri 与 File

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `util.h` 中字符串切分（`strip`/`divide`/`split` 家族）、数字解析（`parseIntNoThrow`/`parseIntSegments`/`getRealSize`）与路径安全（`applyDir`/`fixTaintedBasename`/`detectDirTraversal`）这几组常用工具的分工与行为细节。
2. 解释 `uri.h` 对 URL 的解析模型：`uri_split()` 状态机如何把一个 URI 切成带偏移的字段，`aria2::uri::parse` 又如何把它们装配成 `UriStruct`。
3. 理解 `File` 类对文件属性查询（`stat`）与路径运算（`getBasename`/`getDirname`/`mkdirs`）的封装方式，以及它如何用 `a2stat`/`a2mkdir`/`utf8ToWChar` 把 Windows 与 POSIX 的差异收口。
4. 掌握 `fmt` 的 `vsnprintf` 封装、`percentEncode`/`percentDecode`/`toHex` 与 `base32`/`base64` 两套编码的位级实现。

本讲是第 9 单元（工程实践与二次开发）的第一篇。前面八个单元里，`util::applyDir`、`uri::parse`、`File(...).exists()` 这类调用反复出现但从未展开——本讲就把这些「空气一样的基础设施」集中拆开看一遍。读完之后你再回头看任何一篇讲义里的调用点，都应该能立刻说出它底层做了什么。

## 2. 前置知识

### 2.1 迭代器对（iterator pair）

C++ 标准库用「一对迭代器 `[first, last)`」表示一段区间。aria2 的字符串工具大量采用这种风格：函数返回的不是新字符串，而是**原字符串里的一段位置的起止**。好处是零拷贝——切分一万个片段也不产生一万个临时 `std::string`，需要时再用 `std::string(p.first, p.second)` 物化。看到 `Scip`（`std::pair<std::string::const_iterator, std::string::const_iterator>` 的别名，定义在测试代码中）就是这种产物。

### 2.2 RFC 3986：URI 的语法成分

RFC 3986 把一个 URI 定义为：

```
scheme "://" [ userinfo "@" ] host [ ":" port ] path [ "?" query ] [ "#" fragment ]
```

例如 `http://user:pass@example.com:8080/a/b/file.tar.gz?q=1#frag` 中：

| 成分 | 值 | 说明 |
|---|---|---|
| scheme | `http` | 协议名，大小写不敏感 |
| userinfo | `user:pass` | 字面量凭证，`@` 是它和 host 的分界 |
| host | `example.com` | 可以是域名、IPv4 或 `[IPv6]` 字面量 |
| port | `8080` | 省略时取协议默认端口 |
| path | `/a/b/file.tar.gz` | 必须以 `/` 开头（hier-part 场景） |
| query | `q=1` | `?` 之后、`#` 之前 |
| fragment | `frag` | `#` 之后，客户端通常丢弃 |

**百分号编码（percent-encoding）**：URI 里只允许出现 ASCII 的少数字符，其余字节写成 `%XX`（两位大写十六进制）。RFC 3986 把字符分为 unreserved（字母数字加 `-._~`，无需编码）与 reserved（`: / ? # [ ] @ ! $ & ' ( ) * + , ; =`，有语法含义）。userinfo 里的 `@`、路径里的 `/` 若属于数据本身，就必须编码成 `%40`、`%2F`。

### 2.3 stat 系统调用与 mode

POSIX 的 `stat(path, &st)` 一次调用就能取回文件的存在性、类型（`S_ISREG` 普通文件 / `S_ISDIR` 目录）、大小（`st_size`）与修改时间（`st_mtime`）。`File` 类的所有查询都建立在它之上。

### 2.4 Base32 与 Base64

两者都是把任意字节流映射到「可打印字符集」的编码：

- **Base64**：每 3 字节（24 bit）切 4 组、每组 6 bit，查 64 字符表（`A-Z a-z 0-9 + /`），不足 3 字节用 `=` 填充。
- **Base32**：每 5 字节（40 bit）切 8 组、每组 5 bit，查 32 字符表（`A-Z 2-7`），不足 5 字节用 `=` 填充。字符集更小，代价是编码更长（约为原文的 1.6 倍）。

aria2 里 Base64 用于 HTTP Basic 认证与 WebSocket 握手（u8-l3 的 `Sec-WebSocket-Accept`），Base32 用于 DHT 节点 ID 等二进制数据的展示。

## 3. 本讲源码地图

| 文件 | 规模 | 作用 |
|---|---|---|
| [src/util.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.h) | ~890 行 | 工具函数声明：大量模板（strip/split/divide/streq 家族）直接在头文件实现 |
| [src/util.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc) | ~2600 行 | 非模板实现：percent 编解码、数字解析、路径安全、目录定位等 |
| [src/uri.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.h) / [src/uri.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.cc) | ~400 行 | `UriStruct` 数据模型与 parse/construct/joinUri 三类操作 |
| [src/uri_split.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri_split.h) / [src/uri_split.c](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri_split.c) | ~490 行 | 纯 C 的 URI 切分状态机（零依赖，改自 Joyent 的 http-parser） |
| [src/File.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/File.h) / [src/File.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/File.cc) | ~400 行 | 文件系统对象封装：属性查询、mkdirs、改名、路径拆分 |
| [src/fmt.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/fmt.h) / [src/fmt.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/fmt.cc) | ~60 行 | `fmt()`：返回 `std::string` 的 printf 风格格式化 |
| [src/base32.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/base32.h) / [src/base32.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/base32.cc) | ~100 行 | Base32 编码（encode）与解码（decode 模板） |
| [src/base64.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/base64.h) | ~185 行 | Base64 编解码（全模板，头文件即实现） |
| test/UtilTest1.cc、test/UtilTest2.cc、test/UriTest.cc、test/FileTest.cc | — | 行为的「可执行文档」，本讲多处引用其断言 |

依赖关系：`uri.cc` 依赖 `util.cc`（percentDecode）与 `FeatureConfig.cc`（getDefaultPort）；`File.cc` 依赖 `util.h`、`fmt`；`util`/`File` 又被其余全部子系统依赖——它们位于依赖图的最底层。

## 4. 核心概念与源码讲解

### 4.1 util 工具集

#### 4.1.1 概念说明

`util` 是一个**只装自由函数的命名空间**（`namespace util`），没有类、没有状态，相当于 aria2 自己的 `<algorithm>` 扩展库。按职能可分四组：

1. **字符串处理**：`strip`/`lstripIter`（去首尾空白）、`divide`（按定界符切一刀）、`split`/`splitIter`/`splitIterM`（按定界符全切）、`replace`、大小写与前缀后缀比较（`streq`/`strieq`/`startsWith`/`endsWith` 及其忽略大小写变体）。
2. **数字与单位解析**：`parseIntNoThrow`/`parseLLIntNoThrow`/`parseDoubleNoThrow`（安全版 strtol）、`parseIntSegments`（`1-3,5` 区间语法）、`getRealSize`（`1K`/`1M` 单位换算）、`uitos`/`itos`/`secfmt`/`abbrevSize`（格式化输出）。
3. **路径与文件名安全**：`applyDir`（拼目录）、`detectDirTraversal`（防目录穿越）、`escapePath`/`fixTaintedBasename`/`createSafePath`（文件名消毒）。
4. **环境定位**：`getHomeDir`/`getXDGDir`/`getConfigFile`/`getDHTFile`（配置文件与状态文件的搜索路径）。

为什么 aria2 不直接用 C 标准库？两个动机：**容错语义**（`strtol` 遇到垃圾尾串不报错，aria2 需要「整串要么完整解析、要么失败」的判定）与**零拷贝性能**（下载热路径上每秒切分成千上万段字符串）。

#### 4.1.2 核心流程

util 的字符串工具是一个三层结构，下层为上层服务：

```text
第 1 层  stripIter / lstripIter        返回「区间的新起止」
第 2 层  divide / splitIter / split    在区间上找定界符，产出若干区间
第 3 层  parseIntNoThrow / parseIntSegments / 应用代码   物化并消费区间
```

数字解析的统一骨架是私有模板 `parseLong`（[src/util.cc:L536-L558](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L536-L558)）：

```text
parseLong(res, f, s, base):
  1. s 为空               → false
  2. 调 strtol/strtoll     → endptr 指向第一个未消费字符
  3. errno == ERANGE      → false（溢出）
  4. endptr 之后若还有非空白字符 → false（垃圾尾串）
  5. 否则 true，写入 res
```

三个公开包装只差在范围检查：`parseIntNoThrow` 限 `int32_t` 范围、`parseUIntNoThrow` 限 `[0, INT32_MAX]`、`parseLLIntNoThrow` 不额外限。

路径安全四件套构成一条「文件名消毒流水线」，在 HTTP/FTP 下载确定落盘文件名时被调用（u4-l1 讲过的 HttpResponseCommand 定型文件名环节）：

```text
原始文件名（可能来自 URL、Content-Disposition、percent-decode 后的 basename）
   │
   ├─ isUtf8? ──否──> percentEncode（整体转 %XX）
   │        是
   ▼
fixTaintedBasename:  replace("/", "%2F") 后 escapePath
   │
   ▼
escapePath: 控制字符(0x00-0x1F, 0x7F) → %XX（Windows 下另加 " * : < > ? \ |）
   │
   ▼
applyDir(dir, filename): 拼上 -d 目录，空目录时补 "./"
```

而 `detectDirTraversal` 是独立的守门函数：任何携带 `..`、以 `/` 开头、含控制字符的「相对文件名」都会被判为穿越攻击（恶意种子/服务器可借此把文件写到目录之外）。

#### 4.1.3 源码精读

**（1）strip 家族与默认字符集**

[src/util.cc:L162-L171](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L162-L171)：默认剥离字符集是 `"\r\n\t "`——把 HTTP 头部按行切开后去 `\r\n`、把配置行去空格，靠的都是这一个常量。

```cpp
const char DEFAULT_STRIP_CHARSET[] = "\r\n\t ";

std::string strip(const std::string& str, const char* chars)
{
  std::pair<std::string::const_iterator, std::string::const_iterator> p =
      stripIter(str.begin(), str.end(), chars);
  return std::string(p.first, p.second);
}
```

核心在头文件里的模板 [src/util.h:L116-L130](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.h#L116-L130)：`stripIter` 从两头向中间夹逼，返回新区间；`strip` 只是把它物化成字符串。

**（2）divide：一刀两断**

[src/util.h:L158-L175](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.h#L158-L175)：

```cpp
template <typename InputIterator>
std::pair<std::pair<InputIterator, InputIterator>,
          std::pair<InputIterator, InputIterator>>
divide(InputIterator first, InputIterator last, char delim, bool strip = true)
{
  auto dpos = std::find(first, last, delim);
  if (dpos == last) {
    if (strip) {
      return {stripIter(first, last), {last, last}};
    }
    return {{first, last}, {last, last}};
  }
  ...
}
```

找不到定界符时右半为空区间——调用方据此区分「键值对」与「裸值」。`strip` 默认开启，所以 ` name = value ` 切出来是干净的 `name`/`value`。它的一个直接用户是 `parseIndexPath`（[src/util.cc:L2046-L2057](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L2046-L2057)），解析 `--index-out` 选项的 `1=dir/file` 语法。

**（3）split 家族与 allowEmpty 语义**

三个变体 [src/util.h:L490-L570](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.h#L490-L570)：`splitIter`（产区间）、`splitIterM`（多定界符集）、`split`（产字符串）。注意收尾的这段（`splitIter` 的 L511-L514）：

```cpp
  if (allowEmpty && (first == last || *(last - 1) == delim)) {
    *out++ = std::make_pair(last, last);
  }
```

默认 `allowEmpty=false` 时 `"a,,b"` 会切出 `[a, b]`（空段被丢弃）；`allowEmpty=true` 时尾随定界符还会补一个空段——Cookie 解析等「空字段有语义」的场景需要它。

**（4）安全数字解析三兄弟**

[src/util.cc:L560-L597](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L560-L597)：三者都是 `parseLong` 模板的薄包装。`parseIntNoThrow` 把 `long` 收窄到 `int32_t` 范围，`parseUIntNoThrow` 再加非负约束（注释明确有效范围 `[0, INT32_MAX]`）。u1-l5 讲过的 OptionHandler 数字校验、u3-l4 讲过的 `--select-file` 索引解析，底层都是它们。

**（5）parseIntSegments：区间语法**

[src/util.cc:L626-L665](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L626-L665) 把 `"1-3,5"` 解析成 `SegList<int>`：先按 `,` 分段，每段内再找 `-`，单值 `add(a, a+1)`、区间 `add(a, b+1)`（SegList 是半开区间集合，u5 讲过的 `--select-file` 与 piece 过滤都消费它）。两个失败分支值得注意：`5-` 或 `-5` 这类残缺区间抛 `MSG_INCOMPLETE_RANGE`，无法解析的段抛 `Bad range`。

**（6）getRealSize：1K/1M 单位换算**

[src/util.cc:L1827-L1858](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L1827-L1858)：

```cpp
int64_t getRealSize(const std::string& sizeWithUnit)
{
  std::string::size_type p = sizeWithUnit.find_first_of("KMkm");
  ...
    case 'K': case 'k': mult = 1_k; break;   // 1_k = 1024（字面量运算符）
    case 'M': case 'm': mult = 1_m; break;   // 1_m = 1024*1024
  ...
  if (INT64_MAX / mult < v) {                 // 先除后乘，防溢出
    throw DL_ABORT_EX(...);
  }
  return v * mult;
}
```

注意两点：单位只认 K/M（`-k`、`--min-split-size` 等选项的文档里也只承诺这两个单位）；溢出检查用「`INT64_MAX / mult < v`」先比后乘，避免乘法本身回绕。测试断言见 [test/UtilTest2.cc:L189-L221](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UtilTest2.cc#L189-L221)，其中 `"9223372036854775807K"` 必须抛异常。

**（7）文件名消毒流水线**

- [src/util.cc:L2098-L2123](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L2098-L2123) `applyDir`：处理三种目录形态——空目录补 `./`、根目录 `/` 不重复加斜杠、普通目录补 `/`。
- [src/util.cc:L2162-L2177](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L2162-L2177) `detectDirTraversal`：一行链式判断覆盖 `.`/`..`/绝对路径/尾斜杠/控制字符全部情形，测试 [test/UtilTest2.cc:L806-L822](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UtilTest2.cc#L806-L822) 逐条验证。
- [src/util.cc:L2125-L2128](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L2125-L2128) `fixTaintedBasename` 只有一行：`escapePath(replace(src, "/", "%2F"))`——percent-decode 可能「解出」一个带 `/` 的文件名（如 `file%2Fname`），先把它重新编码回 `%2F`，杜绝文件名里夹带路径分隔符。
- [src/util.cc:L2340-L2353](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L2340-L2353) `createSafePath`：按「是否合法 UTF-8」分流——合法走 fixTaintedBasename，非法整体 percentEncode 后再 escapePath。

**（8）配置文件搜索：getConfigFile 与 getDHTFile**

[src/util.cc:L1806-L1825](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L1806-L1825)：

```cpp
std::string getConfigFile()
{
  std::string filename = getHomeDir() + "/.aria2/aria2.conf";
  if (!File(filename).exists()) {
    filename = getXDGDir("XDG_CONFIG_HOME", getHomeDir() + "/.config") +
               "/aria2/aria2.conf";
  }
  return filename;
}
```

两级探测：传统位置 `~/.aria2/aria2.conf` 优先，不存在再看 XDG（`$XDG_CONFIG_HOME` 或 `~/.config`）。`getDHTFile` 同构，只是换成 `~/.aria2/dht.dat` → `$XDG_CACHE_HOME|~/.cache`/`aria2/dht.dat`——u7-l4 讲过的 DHT 路由表落盘位置正是从这里来的。`getHomeDir`（[src/util.cc:L1746-L1759](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L1746-L1759)）依次尝试 `$HOME`、`getpwuid(geteuid())`，Windows 分支则尝试 `HOME`→`USERPROFILE`→`HOMEDRIVE+HOMEPATH`。

#### 4.1.4 代码实践

**实践目标**：不动手编译也能精确预言 util 字符串/数字函数的行为——方法是用测试断言当「答案卷」。

**操作步骤**：

1. 打开 [test/UtilTest1.cc:L77-L96](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UtilTest1.cc#L77-L96)（`testStrip`）与 [L186-L199](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UtilTest1.cc#L186-L199)（`testDivide`），先遮住断言右侧，只看输入自己推演。
2. 对下列输入分别写出你推演的结果（示例输入，非项目代码）：
   - `util::divide` 处理 `" name = value "`，定界符 `=`
   - `util::split("a,,b", ',', ...)` 在 `allowEmpty=false` 与 `true` 下的段数
   - `util::parseIntSegments("1-3,5")`、`util::getRealSize("1M")`
3. 再打开 [test/UtilTest2.cc:L189-L232](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UtilTest2.cc#L189-L232)（`testGetRealSize`/`testAbbrevSize`）与 [L351-L382](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UtilTest2.cc#L351-L382)（`testParseIntSegments`）核对。
4. （可选，需已按 u1-l2 完成构建）运行 `make check` 确认这些用例在当前 HEAD 上全部通过。

**需要观察的现象**：`divide` 对 `" name = value "` 的返回值两侧都已去空白；`getRealSize` 对 `""`、`"foo"`、`"-1"`、`"9223372036854775807K"` 全部抛异常而不是返回错误码。

**预期结果**：`divide` 产出 `("name", "value")`；`parseIntSegments("1-3,5")` 得到覆盖 1,2,3,5 的 SegList；`getRealSize("1M")` = 1048576。（推演结果如与断言不一致，回到 4.1.3 对应函数重读。）

#### 4.1.5 小练习与答案

**练习 1**：`util::parseIntNoThrow(res, "123abc")` 返回什么？为什么 aria2 不直接用 `atoi`？

答案：返回 `false`。`parseLong` 检查 `endptr` 之后不允许出现非空白字符，`"abc"` 属于垃圾尾串，整个解析被判失败。`atoi` 遇到垃圾尾串会静默返回 123，且无法报告溢出（UB），对配置解析来说等于埋雷。

**练习 2**：`util::fixTaintedBasename("a/b")` 返回什么？这个函数防的是什么攻击？

答案：返回 `"a%2Fb"`（断言见 [test/UtilTest2.cc:L789-L797](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UtilTest2.cc#L789-L797)）。恶意服务器可把下载文件名写成 `../../.ssh/authorized_keys` 这类内容；percent-decode 之后文件名里会出现 `/` 与 `..`，若不重新消毒，落盘路径就逃出了 `-d` 目录。`fixTaintedBasename` 把文件名里的 `/` 编回 `%2F`，再由 `detectDirTraversal` 在更外层（如 bittorrent_helper 装配 FileEntry 时，u6-l1）拦截穿越路径。

**练习 3**：为什么 `getRealSize` 的溢出检查写成 `INT64_MAX / mult < v`，而不是先 `v * mult` 再判断？

答案：`v * mult` 本身就可能已经回绕（有符号整数溢出是未定义行为，实际常表现为负数），之后再判断得到的结论不可靠。「先用极限除以乘数、再与被乘数比较」能在不发生溢出的前提下预判乘法结果是否越界。

### 4.2 uri 解析

#### 4.2.1 概念说明

aria2 把「解析一个 URL」拆成两层：

- **底层 `uri_split()`**（[src/uri_split.c](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri_split.c)）：纯 C 状态机，输入一个字符串，输出「每个字段在原串中的偏移+长度」，**不分配任何内存、不修改原串**。文件头注释说明它基于 Joyent 的 http-parser 改写。
- **上层 `aria2::uri::parse()`**（[src/uri.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.cc)）：拿着 `uri_split` 的偏移结果，把各字段物化成 `UriStruct` 的九个 `std::string`/整数成员，并补三件事——默认端口回填、path 拆成 dir+file、userinfo 做 percentDecode。

`UriStruct`（[src/uri.h:L48-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.h#L48-L66)）是全项目通用的 URL 模型，Request 类（u4-l1）内部就持有一份。注意它**没有 fragment 字段**——aria2 作为客户端用不到 fragment，`uri_split` 明明切出来了也直接丢弃。

除 parse 外，uri 命名空间还提供三件配套工具：`construct`（UriStruct → 字符串，parse 的逆）、`joinUri`（相对引用解析，HTML 页面里遇到 `./next.html` 时拼出绝对 URL，服务 Metalink 与 Content-Disposition 场景）、`normalizePath`（消解 `.`/`..` 与重复斜杠）。

#### 4.2.2 核心流程

**uri_split 的字段表示**（[src/uri_split.h:L45-L73](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri_split.h#L45-L73)）：十个字段枚举（`USR_SCHEME`…`USR_BASENAME`）对应 `field_set` 位图中的十个位；每个出现的字段记录 `(off, len)` 二元组；端口是特例——只置 `field_set` 位、数值存独立的 `res->port`。

**状态机主流程**（单趟扫描，无回溯）：

```text
URI_BEFORE_SCHEME → URI_SCHEME(遇':') → SLASH1 → SLASH2(即 "//")
  → BEFORE_MAYBE_USER → MAYBE_USER(遇':'转 PASSWD, 遇'@'记 last_atmark)
  → BEFORE_MAYBE_PASSWD → MAYBE_PASSWD(遇'@'更新 last_atmark)
  → 遇'/' 或 '[' 时结算 host：
      host_first = last_atmark+1（userinfo 存在时），user_last = last_atmark
  → URI_HOST / URI_BEFORE_IPV6HOST('[') / URI_PORT(':')
  → URI_PATH(遇'?'转 QUERY, 遇'#'转 FRAGMENT)
最后统一 uri_set_field 写回十个字段（uri_split.c:468-490）
```

**@ 歧义的消解规则**：userinfo 和 host 都可能含 `@`（IPv6 不含，但用户名可以是邮箱）。`uri_split` 的规则是**以最后一个 `@`（last_atmark）为界**，`@` 之前是 userinfo、之后是 host。测试 `testSetUri_usernamePassword`（[test/UriTest.cc:L328-L346](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UriTest.cc#L328-L346)）里那个 `ftp://aria2@user%40:aria2@pass%40@localhost/...` 的怪例，host 得到的就是 `pass%40@localhost`——最后一个 `@` 之后的整段，即便看起来不合法。

**parse 的装配规则**（细节见 4.2.3 源码）：

```text
uri::parse(result, uri):
  uri_split 失败              → return false
  无端口 → getDefaultPort(protocol) 为 0（未知协议）→ return false
  path 存在且 basename 存在   → dir = path 去尾段, file = 尾段
  path 存在但以 '/' 结尾      → dir = path, file = ""
  无 path                    → dir = "/", file = ""
  query 存在                 → query = "?" + 原文（保留前导 '?'）
  user/passwd 存在           → 各做 percentDecode，hasPassword 置位
  flags & USF_IPV6ADDR       → ipv6LiteralAddress = true（host 不含方括号）
```

#### 4.2.3 源码精读

**（1）UriStruct 数据模型**

[src/uri.h:L48-L66](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.h#L48-L66)：九个字段——protocol、host、dir、file、query、username、password、port（`uint16_t`）、三个布尔（hasPassword/ipv6LiteralAddress）。手写拷贝赋值与 `swap`（[src/uri.cc:L52-L86](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.cc#L52-L86)）逐成员交换，是 C++11 之前的习惯写法。

**（2）默认端口回填与「协议白名单」效应**

[src/uri.cc:L98-L110](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.cc#L98-L110)：

```cpp
result.protocol.assign(p + res.fields[USR_SCHEME].off, res.fields[USR_SCHEME].len);
result.host.assign(p + res.fields[USR_HOST].off, res.fields[USR_HOST].len);
if (res.port == 0) {
  uint16_t defPort;
  if ((defPort = getDefaultPort(result.protocol)) == 0) {
    return false;
  }
  result.port = defPort;
}
```

`getDefaultPort` 在 [src/FeatureConfig.cc:L77-L94](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/FeatureConfig.cc#L77-L94) 只认 http/https/ftp/sftp 四个协议，其余返回 0。于是得到一个重要推论：**`uri::parse` 只接受这四个协议（或显式写了端口的情况）**。`magnet:?xt=...`、`file:///...` 并不经过 `uri::parse`——u3-l3 的 ProtocolDetector 嗅探 scheme 用的是更宽容的 `uri_split`（只要求 `scheme://` 结构成立），magnet 有专门的 `magnet.cc`（u6-l9）。

**（3）dir/file 拆分**

[src/uri.cc:L112-L128](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.cc#L112-L128)：`uri_split` 交出的 `USR_PATH` 是完整路径、`USR_BASENAME` 是最后一个斜杠之后的尾段（`uri_split.c:L483-L485`：仅当路径不以 `/` 结尾才设置）。于是 `dir = PATH` 长度减去 `BASENAME` 长度的前缀。对照断言（[test/UriTest.cc:L106-L118](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UriTest.cc#L106-L118)）：`http://host/aria2/index.html` → dir=`/aria2/`、file=`index.html`；`http://host/aria2/` → dir=`/aria2/`、file=`""`；无路径时 dir=`"/"`。

**（4）query 与 userinfo 的不对称处理**

[src/uri.cc:L130-L159](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.cc#L130-L159)：query 原样保留并**补上前导 `?`**（`result.query = "?"; result.query.append(...)`）；username/password 则各做一次 `util::percentDecode`——也就是说 `UriStruct` 里的凭证是**解码后的明文**，construct 时再编码回去。fragment 在这一步被直接无视。

**（5）construct：parse 的逆**

[src/uri.cc:L174-L209](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.cc#L174-L209)：

```cpp
  if (!us.username.empty()) {
    res += util::percentEncode(us.username);
    if (us.hasPassword) {
      res += ":";
      res += util::percentEncode(us.password);
    }
    res += "@";
  }
  if (us.ipv6LiteralAddress) {
    res += "[";
    res += us.host;
    res += "]";
  }
  ...
  uint16_t defPort = getDefaultPort(us.protocol);
  if (us.port != 0 && defPort != us.port) {
    res += fmt(":%u", us.port);
  }
  res += us.dir;
  if (us.dir.empty() || us.dir[us.dir.size() - 1] != '/') {
    res += "/";
  }
  res += us.file;
  res += us.query;
```

三个规则：IPv6 host 要重新套上方括号（parse 时剥掉了）；端口等于协议默认端口时**省略**（规范化输出）；dir 末尾保证 `/` 再接 file。username/password 用 percentEncode 编回（与 parse 的 percentDecode 成对）。

**（6）normalizePath：原地压缩的状态机**

[src/uri.cc:L211-L328](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.cc#L211-L328) 用五个状态（`NPATH_START/SLASH/SDOT/DDOT/PATHCOMP`）单趟扫描，把要保留的字节区间（成对记录进 `range`）挑出来，再一次性 `std::copy` 回字符串前部、`erase` 尾部——不分配新串。`..` 的处理在 [L272-L284](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.cc#L272-L284)：遇到 `/` 时连续 `pop_back()` 最多 3 次，恰好弹掉「前一分量的起点、终点和 `..` 自己已压栈的起点」；若栈里不足（路径以 `../` 开头），`!range.empty()` 保护下只会弹掉 `..` 自身——对应头文件注释承诺的「不够弹的 `..` 直接丢弃」。

**（7）joinUri：相对引用解析**

[src/uri.cc:L359-L395](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.cc#L359-L395)：若 uri 本身能被 parse（是绝对 URI）则原样返回；否则先截掉 `#` 之后（fragment 丢弃）与 `?` 之后（query 暂存），把剩余相对路径与 `baseUri` 的 dir 做 `joinPath`（内部调 `normalizePath`），再用 `construct` 重组、把 query 拼回。 Metalink/HTML 场景中相对链接就是这么变成绝对 URL 的。

#### 4.2.4 代码实践

**实践目标**：亲手驱动一遍 `uri_split`，对一个含 userinfo/query/fragment 的 URL 逐字段打印切分结果，并与 RFC 3986 的成分表对照。

**为什么这样做而不是直接调 `aria2::uri::parse`**：`uri.cc` 依赖 `util.cc`、`FeatureConfig.cc`，而 `util.cc` 又牵出 RequestGroup/Option 等一大批依赖，无法单独编译；`uri_split.c` 是零依赖的纯 C 文件（只 include `uri_split.h` 与 `<stdlib.h>`，见 [src/uri_split.c:L35-L37](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri_split.c#L35-L37)），且 `uri_split.h` 自带 `extern "C"` 保护（[L38-L40](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri_split.h#L38-L40)），可以和一个小 C++ 程序直接链接——这正是任务规格里「链接 util.cc（或直接阅读后手写等价逻辑）」的后一条路线。

**操作步骤**：

1. 在仓库外任选一个临时目录，新建 `uri_probe.cc`（以下为示例代码）：

   ```cpp
   // 示例代码：uri_probe.cc —— 复用 aria2 的 uri_split.c 观察切分结果
   #include <cstdio>
   #include <string>
   #include "uri_split.h"

   static std::string field(const uri_split_result& res, int f, const char* base)
   {
     if (res.field_set & (1 << f)) {
       return std::string(base + res.fields[f].off, res.fields[f].len);
     }
     return "<absent>";
   }

   int main(int argc, char** argv)
   {
     const char* uri = argc > 1 ? argv[1]
         : "http://user%40x:p%40w@example.com:8080/a/b/file.tar.gz?q=1#frag";
     uri_split_result res;
     if (uri_split(&res, uri) != 0) {
       std::printf("uri_split failed\n");
       return 1;
     }
     static const char* names[] = {"SCHEME",   "HOST",    "PORT",  "PATH",
                                   "QUERY",    "FRAGMENT", "USERINFO",
                                   "USER",     "PASSWD",  "BASENAME"};
     for (int f = 0; f < USR_MAX; ++f) {
       if (f == USR_PORT) { // USR_PORT 只置位不填 off/len，必须读 res.port
         std::printf("%-9s = %u\n", names[f], res.port);
         continue;
       }
       std::printf("%-9s = %s\n", names[f], field(res, f, uri).c_str());
     }
     std::printf("ipv6       = %d\n", (res.flags & USF_IPV6ADDR) != 0);
     return 0;
   }
   ```

2. 编译（在 aria2 仓库根目录执行，`-I src` 让 `#include "uri_split.h"` 可达）：

   ```console
   $ g++ -std=c++11 -Wall -I src /path/to/uri_probe.cc src/uri_split.c -o /tmp/uri_probe
   ```

3. 运行默认用例，再分别试这两个输入：
   - `/tmp/uri_probe 'ftp://aria2@user@localhost/download/aria2-1.0.0.tar.bz2'`（多个 `@`）
   - `/tmp/uri_probe 'http://[2001:db8::1]:6881/dir/file'`（IPv6 字面量）

**需要观察的现象**：

- 十个字段中哪些出现在 `field_set` 里、哪些是 `<absent>`；`USR_PORT` 的特殊性（置位但不填 off/len，见 [src/uri_split.c:L486-L489](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri_split.c#L486-L489)）。
- `USERINFO` 恰好等于 `USER:PASSWD`（不含 `@`），与「最后一个 `@` 为界」的规则一致。
- `FRAGMENT = frag` 能被 `uri_split` 切出来，但 `UriStruct` 没有对应字段（aria2 丢弃它）。
- 多 `@` 用例中 `USER = aria2@user`、`HOST = localhost`，与 [test/UriTest.cc:L314-L326](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UriTest.cc#L314-L326) 的断言一致。
- IPv6 用例中 `HOST = 2001:db8::1`（不含方括号）、`ipv6 = 1`，对应 [test/UriTest.cc:L348-L361](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UriTest.cc#L348-L361)。

**预期结果**（默认用例，基于源码推演，待本地验证）：

```text
SCHEME     = http
HOST       = example.com
PORT       = 8080
PATH       = /a/b/file.tar.gz
QUERY      = q=1
FRAGMENT   = frag
USERINFO   = user%40x:p%40w
USER       = user%40x
PASSWD     = p%40w
BASENAME   = file.tar.gz
ipv6       = 0
```

再对照 4.2.2 的 parse 装配规则手推 `UriStruct`：port=8080（显式）、dir=`/a/b/`、file=`file.tar.gz`、query=`?q=1`、username=`user@x`（解码后）、password=`p@w`、hasPassword=true。**注意 `user%40x` 里的 `%40` 经 percentDecode 变成了 `@`**——这正是 4.2.3（4）所说的不对称处理。

#### 4.2.5 小练习与答案

**练习 1**：`aria2::uri::parse` 解析 `http://example.com/` 时 port 是多少？解析 `myproto://example.com/`（不带端口）呢？

答案：前者 port=80，`getDefaultPort("http")` 回填；后者 parse 直接返回 `false`——`myproto` 不在 http/https/ftp/sftp 白名单里，`getDefaultPort` 返回 0（[src/uri.cc:L101-L107](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.cc#L101-L107)）。所以 aria2 判断「这是不是可下载的流协议 URI」依赖 scheme 白名单，而非任意 URL。

**练习 2**：`uri_split` 为什么给 `USR_PORT` 破例，不填 `fields[USR_PORT].off/len` 而是单独存 `res->port`？

答案：端口是数字不是文本切片。其他字段都能用「原串中的 (off, len)」表达，端口则要完成字符串→数值的解析（`uri_split` 内部边扫边算），存成 `uint16_t` 后，上层（`uri::parse`）无需再解析一次。这也解释了实践程序为什么必须特判 `USR_PORT`。

**练习 3**：`uri::construct` 对 `http://example.com:80/a/` 的输出是什么？与输入相比丢了什么？

答案：输出 `http://example.com/a/`。端口 80 是 http 的默认端口，construct 的省略规则（[src/uri.cc:L196-L199](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.cc#L196-L199)）会把 `:80` 规范化掉；同理 `:443`/`:21`/`:22` 也会被省略。parse→construct 是「规范化」而非「保形」往返。

### 4.3 File 封装

#### 4.3.1 概念说明

`File`（[src/File.h:L50-L129](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/File.h#L50-L129)）是一个**只持有一个 `std::string name_` 的值类型**：构造时只记住路径，不做任何 I/O；每次查询（exists/isFile/isDir/size/getModifiedTime）都现调一次 `fillStat`→`a2stat`。它不代表「打开的文件」（那是 DiskWriter 的职责，u5-l1），只代表「路径」。

它的存在价值是三件事：

1. **把 stat 族调用收口**：全项目所有「这个文件存在吗/多大/是不是目录」的判断都经 `File::fillStat` 单点（[src/File.cc:L71-L74](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/File.cc#L71-L74)）。
2. **平台差异翻译**：`a2stat`/`a2mkdir`/`a2unlink`/`a2rmdir` 来自平台兼容层 `a2io.h`；Windows 下所有路径先经 `utf8ToWChar` 转宽字符（util.h 中 MinGW 分支定义，非 MinGW 下是恒等宏）。u1-l2 讲过的「一份源码任意裁剪」在文件系统层就体现为这些小别名。
3. **路径运算**：`getBasename`/`getDirname` 拆路径、`getCurrentDir` 取工作目录、`getPathSeparators` 按平台返回 `/` 或 `/\`。

#### 4.3.2 核心流程

查询类方法全部是同一个骨架：

```text
exists/isFile/isDir/size/mode/getModifiedTime:
  fillStat(fstat)            // = a2stat(utf8ToWChar(name_), &fstat)
    失败 → 按各自约定返回 false/0
    成功 → 按 S_ISREG/S_ISDIR/st_size/st_mtime 解释
```

`mkdirs` 的逐段创建算法（[src/File.cc:L163-L245](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/File.cc#L163-L245)）：

```text
已 isDir → return false（"已存在也算失败"，由调用方复核）
从左到右扫描路径串，每遇到一个 '/' 得到一个前缀 dir：
  File(dir).isDir() 已存在 → continue
  a2mkdir(dir, DIR_OPEN_MODE) 失败 → return false
全部成功 → return true
```

util 侧的包装 `util::mkdirs`（[src/util.cc:L1942-L1954](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L1942-L1954)）专门消化这个「已存在也返回 false」的语义：失败后再复查 `isDir()`，仍是目录就视为成功，否则才抛 `DIR_CREATE_ERROR`。

#### 4.3.3 源码精读

**（1）fillStat：唯一的 stat 出口**

[src/File.cc:L71-L80](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/File.cc#L71-L80)：

```cpp
int File::fillStat(a2_struct_stat& fstat)
{
  return a2stat(utf8ToWChar(name_).c_str(), &fstat);
}

bool File::exists()
{
  a2_struct_stat fstat;
  return fillStat(fstat) == 0;
}
```

`a2_struct_stat`/`a2stat` 在 POSIX 下就是 `struct stat`/`stat`，Windows 下映射到 `_wstati64`。注意 `exists()` 不区分「不存在」和「无权限」——查询失败一律当不存在，这个保守语义贯穿全项目。

**（2）size 的 Windows 特例**

[src/File.cc:L137-L161](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/File.cc#L137-L161)：MinGW 分支不用 `_wstat` 而是开句柄 `GetFileSizeEx`，因为 `_wstat` 对符号链接永远返回 0（代码注释引用了 MSDN 原文）。这是「同一接口、按平台换实现」的典型案例。

**（3）getBasename/getDirname 的边界处理**

[src/File.cc:L256-L286](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/File.cc#L256-L286)：`getBasename` 找最后一个分隔符取后缀；`getDirname` 取前缀，且有三个边界——无分隔符时返回 `"."`（相对路径的父目录约定）、分隔符在第 0 位（`/foo`）返回 `"/"`、空串返回空。下载器里「URL 尾段 → 落盘文件名、其余 → 目录」的拆分用的就是这一对。

**（4）renameTo 与原子写**

[src/File.cc:L290-L312](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/File.cc#L290-L312)：POSIX 用 `rename()`（目标存在时原子替换），Windows 用 `MoveFileExW(MOVEFILE_REPLACE_EXISTING)`（注释点名 MinGW 的 `rename` 不删已存在目标）。util::saveAs（[src/util.cc:L2075-L2096](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L2075-L2096)）把它与「先写临时文件」组合成原子保存——`filename + "__temp"` 写完再 rename，u6-l11 讲过的 `.aria2` 控制文件原子落盘、u7-l4 的 dht.dat 保存用的正是这条链。

**（5）mkdirs 的逐段推进**

[src/File.cc:L214-L244](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/File.cc#L214-L244)（POSIX 主体）：

```cpp
  for (std::string::iterator i = begin; i != end;) {
    std::string::iterator j = std::find(i, end, '/');
    if (std::distance(i, j) == 0) {
      ++i;
      continue;                    // 连续 '/' 跳过（含开头的 '/'）
    }
    i = j;
    if (i != end) {
      ++i;
    }
    std::string dir(begin, j);
    if (File(dir).isDir()) {
      continue;                    // 已存在，跳过
    }
    if (a2mkdir(utf8ToWChar(dir).c_str(), DIR_OPEN_MODE) == -1) {
      return false;
    }
  }
  return true;
```

`/a/b/c` 会被依次尝试创建 `/a`、`/a/b`、`/a/b/c`——不需要系统提供 `mkdir -p`。Windows 分支额外处理 UNC 路径（`//host/share/...`）要先跳过主机段再逐段创建。

#### 4.3.4 代码实践

**实践目标**：观察 `File` + `util::getDHTFile`/`getConfigFile` 如何合作完成「两级配置探测」，并验证 `mkdirs` 的逐段创建与「已存在返回 false」语义。

**操作步骤**：

1. 阅读 [src/util.cc:L1816-L1825](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L1816-L1825)（getDHTFile），列出它探测的两个候选路径。
2. 在机器上检查这两个路径是否存在（示例命令）：

   ```console
   $ ls -l ~/.aria2/dht.dat "${XDG_CACHE_HOME:-$HOME/.cache}/aria2/dht.dat" 2>&1
   ```

3. 若你跑过 u7-l4 的 DHT 实验、dht.dat 已生成，把它移动到另一个候选位置后再启动 `./src/aria2c --enable-dht ...`，观察日志从哪个文件加载路由表。
4. 对照 [test/FileTest.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/FileTest.cc) 中 mkdirs 相关断言，回答：对已存在的目录调用 `File::mkdirs()` 返回值是什么？`util::mkdirs` 为什么不直接传播它？

**需要观察的现象**：`~/.aria2/` 存在时 dht.dat 从旧位置读取；旧位置缺失时走 XDG 位置；两个位置都没有时 aria2 冷启动并在（默认 XDG 位置）重建。

**预期结果**：探测顺序为 `~/.aria2/dht.dat` → `$XDG_CACHE_HOME|~/.cache`/`aria2/dht.dat`；`File::mkdirs` 对已存在目录返回 `false`，因此 `util::mkdirs` 必须用「失败后复查 isDir」把「已存在」还原成成功（[src/util.cc:L1945-L1952](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L1945-L1952)）。路径移动行为待本地验证（依赖你是否运行过 DHT）。

#### 4.3.5 小练习与答案

**练习 1**：`File("/nonexistent/path")` 的构造会发生什么？随后调用 `exists()`、`size()`、`getModifiedTime()` 各返回什么？

答案：构造只把路径存进 `name_`，不发生任何系统调用；`exists()` 返回 false（stat 失败非 0）；`size()` 返回 0；`getModifiedTime()` 返回代表 epoch 0 的 Time 对象（[src/File.cc:L366-L373](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/File.cc#L366-L373)）。查询失败全部退化为「假/零」而非异常。

**练习 2**：为什么 `util::saveAs` 要先写 `filename + "__temp"` 再 `renameTo`？

答案：直接写目标文件，进程中途崩溃会留下半截文件；先写临时名再 rename，目标文件要么是旧内容、要么是完整新内容（POSIX rename 的原子性），不会出现中间态。aria2 所有「落盘必须不半途而废」的场合（控制文件、dht.dat、会话文件）都复用这个模式。

**练习 3**：`File("foo/bar.txt").getDirname()` 与 `File("/foo").getDirname()` 分别返回什么？

答案：`foo/bar.txt` 含分隔符且分隔符不在开头，返回纯前缀 `"foo"`；`/foo` 的分隔符恰在第 0 位，返回 `"/"`。易错点是第三种情形：完全不含分隔符时（如 `File("foo")`）`getDirname` 返回的是 `"."` 而不是空串。规则见 [src/File.cc:L268-L286](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/File.cc#L268-L286)。

### 4.4 格式化与编码

#### 4.4.1 概念说明

这一模块收拢三块小而通用的能力：

1. **`fmt()`**：printf 风格但直接返回 `std::string`。aria2 代码里 `fmt("%s: %d", ...)` 的出现频率极高，它解决的是 `snprintf` 需要手工管理缓冲区、`std::ostringstream` 啰嗦的两个痛点。
2. **字节级编码**：`percentEncode`/`percentDecode`（URI）、`torrentPercentEncode`（BT tracker 参数）、`toHex`/`fromHex`（十六进制，infohash 展示）、`base32::encode/decode`、`base64::encode/decode`。它们的共同基础是 `util.h`/`util.cc` 里的一组字符分类函数。
3. **数字格式化**：`uitos(n, true)`（带千分位逗号）、`secfmt`（`1h2m3s`）、`abbrevSize`（`1.5Gi`）——控制台进度行（u3-l5 的 StatCalc）的显示层。

#### 4.4.2 核心流程

**fmt 的实现**（[src/fmt.cc:L43-L63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/fmt.cc#L43-L63)）：

```text
va_list 收参 → vsnprintf 到 2048 字节栈缓冲 → 失败(负值)置空串 → return std::string(buf)
```

**percentEncode 的判定基础**是 RFC 3986 字符表（[src/util.cc:L256-L270](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L256-L270)）：reserved 18 字符、unreserved = 字母数字 + `-._~`。编码规则：**非 unreserved 一律 `%XX` 大写十六进制**（[src/util.cc:L383-L395](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L383-L395)）。

**Base32 的位打包**：把字节流看成一条位流，每 5 bit 一组映射一个字符。5 字节恰好 40 bit = 8 组，无填充；尾部不足时左移到 5 的倍数位、查表输出后用 `=` 补齐到 8 的倍数字符：

\[ \text{输出字符数} = \left\lceil \frac{8 \times \text{字节数}}{5} \right\rceil \text{（再向上取整到 8 的倍数，差额补 = ）} \]

对应关系（[src/base32.cc:L68-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/base32.cc#L68-L93)）：剩 1 字节→2 字符+6 个 `=`，2 字节→4+4，3 字节→5+3，4 字节→7+1。

**Base64 同理**，粒度换为 3 字节→4 个 6 bit 组，`=` 至多 2 个。

#### 4.4.3 源码精读

**（1）fmt：一行核心与一个编译期护栏**

[src/fmt.h:L45-L51](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/fmt.h#L45-L51) 给 `fmt` 挂了 `__attribute__((format(printf, 1, 2)))`——格式串与可变参的类型不匹配会直接编译告警，把 printf 系最常见的运行期崩溃拦在编译期。[src/fmt.cc:L43-L63](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/fmt.cc#L43-L63) 的实现体只有一个 2048 字节栈缓冲；这意味着**格式化结果超过 2047 字节会被截断**（MinGW 下 `vsnprintf` 截断返回 -1 的分支单独处理）。日志与错误消息够用，但不要用它拼大块数据。

**（2）percentEncode：只认 unreserved**

[src/util.cc:L383-L395](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L383-L395)：

```cpp
std::string percentEncode(const unsigned char* target, size_t len)
{
  std::string dest;
  for (size_t i = 0; i < len; ++i) {
    if (inRFC3986UnreservedChars(target[i])) {
      dest += target[i];
    }
    else {
      dest.append(fmt("%%%02X", target[i]));
    }
  }
  return dest;
}
```

`std::string` 重载先整体扫一遍（[L397-L405](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L397-L405)），全是 unreserved 就原样返回、避免分配。兄弟函数 `torrentPercentEncode`（[L425-L443](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L425-L443)）只保留字母数字——BT tracker 约定的编码更保守，`-._~` 也要编码，u6-l7 的 announce URL 组装用它编码 info_hash/peer_id。

**（3）percentDecode：宽容解码**

[src/util.cc:L445-L466](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L445-L466)：`%` 后跟两个合法十六进制位才解码；`%5t`、`%`、`%3` 这类残缺序列**原样保留**而不是报错（测试 [test/UtilTest2.cc:L172-L186](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/UtilTest2.cc#L172-L186)）。理由：percentDecode 的输入可能本来就不是编码过的文本（比如服务器原始响应里的字面 `%`）。

**（4）toHex 与 hexCharToUInt：小写输出、255 哨兵**

[src/util.cc:L468-L487](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L468-L487) `toHex` 每字节产出两个字符，`>= 10` 的半字节加 `'a' - 10`——所以 aria2 显示的 infohash 全是小写。反向的 `hexCharToUInt`（[L499-L516](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L499-L516)）对非法字符返回 255 作哨兵；头文件模板 `fromHex`（[src/util.h:L254-L272](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.h#L254-L272)）据此判断「长度非偶数或含非法字符→返回空串」。

**（5）base32::encode：位累积器**

[src/base32.cc:L43-L95](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/base32.cc#L43-L95) 用一个 `uint64_t buf` 做位累积器：每读一字节 `buf <<= 8; buf += ch`，攒满 5 字节就按 5 bit 一组从低位取出 8 个索引查 `B32TABLE`（表内容 `A-Z2-7`，[L43-L46](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/base32.cc#L43-L46)）。尾部四种残余量各自左移到对齐后取整组、再补 `=`（[L68-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/base32.cc#L68-L93)）。解码是头文件模板（[src/base32.h:L50-L93](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/base32.h#L50-L93)）：长度非 8 的倍数直接拒绝；每 8 字符（40 bit）取高 40 位还原 5 字节，遇 `=` 停止；任何非法字符清空返回。

**（6）base64：encode/decode 全在头文件**

[src/base64.h:L44-L90](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/base64.h#L44-L90) encode：3 字节装进 `int n`（24 bit），四次右移取 6 bit 查 `CHAR_TABLE`；余 2 字节补 1 个 `=`，余 1 字节补 2 个。[L103-L179](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/base64.h#L103-L179) decode 用 256 项 `INDEX_TABLE`（非法 -1）反查，`getNext` 会**跳过**表中为 -1 且非 `=` 的字符——所以带换行的 MIME 风格 Base64 也能解。`=` 只允许出现在第 3、4 位（`eq <= 2` 视为非法，整串清空，[L152-L156](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/base64.h#L152-L156)）。u4-l3 的 HTTP Basic 认证头 `Authorization: Basic base64(user:pass)` 用的就是 `base64::encode`。

**（7）数字格式化三件套**

- `uitos(n, comma)` 模板（[src/util.h:L177-L200](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.h#L177-L200)）：纯除法/取模手写转换，`comma=true` 时每三位插一个 `,`；`itos`（[src/util.cc:L173-L195](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L173-L195)）处理负号并特判 `INT64_MIN`。
- `secfmt`（[src/util.cc:L518-L534](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L518-L534)）：`3661` → `"1h1m1s"`；零输入得 `"0s"`（`tsec == 0` 分支）。
- `abbrevSize`（[src/util.cc:L1860-L1886](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L1860-L1886)）：循环除 1024 选出 `""/Ki/Mi/Gi` 单位，`t >= 922` 时进一位单位避免四舍五入越界，`t < 10 && uidx > 0` 时补一位小数（`r * 10 / 1024`）——控制台里 `1.5GiB` 风格的显示即出于此。

#### 4.4.4 代码实践

**实践目标**：用系统工具交叉验证 aria2 的编码函数输出与标准 Base32/Base64/hex 一致，建立「读到源码就能预言输出」的信心。

**操作步骤**：

1. 对字符串 `aria2` 手推三重身份（示例数据）：
   - `util::toHex("aria2")`（每字节两个小写十六进制字符）
   - `base64::encode(...)`（4 字节 → 6 字符 + 2 个 `=`）
   - `base32::encode(...)`（4 字节 → 7 字符 + 1 个 `=`）
2. 用命令行工具核对（示例命令）：

   ```console
   $ printf '%s' 'aria2' | xxd -p          # toHex：期望 6172696132
   $ printf '%s' 'aria2' | base64          # base64：期望以 == 结尾
   $ printf '%s' 'aria2' | base32          # base32：期望以单个 = 结尾
   ```

3. 阅读 [test/Base32Test.cc](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/test/Base32Test.cc)（56 行，很短）核对第 1 步的 base32 推演。
4. 思考并验证：`percentEncode("a b/c")` 的输出是什么？（提示：空格与 `/` 都不是 unreserved。）

**需要观察的现象**：三个系统工具的输出与你按位推演的结果完全一致；aria2 的实现没有任何私有变体（Base32 依 RFC 4648、Base64 依 RFC 4648 标准字母表）。

**预期结果**：`toHex("aria2") = "6172696132"`；`percentEncode("a b/c") = "a%20b%2Fc"`（空格→`%20`、`/`→`%2F`）；base64/base32 结果以 `=` 填充，长度符合 4.4.2 的公式。（`xxd -p` 会输出无填充小写 hex；base32/base64 命令的具体串待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：`fmt()` 为什么不适合拼接超过 2047 字节的内容？代码依据在哪？

答案：[src/fmt.cc:L47-L49](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/fmt.cc#L47-L49) 用固定 2048 字节栈缓冲调 `vsnprintf`，超出部分被截断（且函数不知道被截了多少）。日志、错误消息、短 URI 拼接都远小于这个量级，因此设计上可以接受。

**练习 2**：`util::percentEncode('/')` 和 `util::torrentPercentEncode('/')` 的输出分别是什么？为什么不同？

答案：前者 `"%2F"`（`/` 属 reserved、非 unreserved，[src/util.cc:L383-L395](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L383-L395)）；后者也是 `"%2F"`，但原因不同——`torrentPercentEncode` 只保留字母数字（[src/util.cc:L425-L437](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L425-L437)），连 `-._~` 都会编码。差异的典型体现是 `~`：percentEncode 原样保留，torrentPercentEncode 编码为 `%7E`。BT tracker 的 BEP 约定要求后者这种更保守的编码。

**练习 3**：`util::abbrevSize(1536)` 返回什么？

答案：`"1.5Ki"`。1536 除以 1024 商 1 余 512，单位下标到 `Ki`；`t=1 < 10` 触发小数分支，`r * 10 / 1024 = 512*10/1024 = 5`，拼出 `1.5` 加单位 `Ki`（[src/util.cc:L1878-L1885](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/util.cc#L1878-L1885)）。

## 5. 综合实践

**任务**：把 4.2 的探针升级成「迷你 UriStruct 引擎」——用 `uri_split.c` 加上你手写的等价逻辑，完整复刻 `aria2::uri::parse` 与 `aria2::uri::construct` 的行为，并做往返一致性检验。这个任务串起本讲全部四个模块：uri 状态机（4.2）、percentDecode（4.4）、默认端口表（FeatureConfig 的手写等价）、fmt 式的输出格式化（4.4）。

**操作步骤**：

1. 在 4.2 的 `uri_probe.cc` 基础上增加（以下均为示例代码）：

   ```cpp
   // 手写 util::percentDecode 的等价逻辑（对照 util.cc:445-466）
   static int hexval(char c)
   {
     if ('0' <= c && c <= '9') return c - '0';
     if ('a' <= c && c <= 'f') return c - 'a' + 10;
     if ('A' <= c && c <= 'F') return c - 'A' + 10;
     return -1;
   }
   static std::string percentDecode(const std::string& s)
   {
     std::string out;
     for (size_t i = 0; i < s.size(); ++i) {
       if (s[i] == '%' && i + 2 < s.size() /* 语义见注 */) {
         int h = hexval(s[i + 1]), l = hexval(s[i + 2]);
         if (h >= 0 && l >= 0) {
           out += static_cast<char>(h * 16 + l);
           i += 2;
           continue;
         }
       }
       out += s[i];
     }
     return out;
   }
   // 注意：aria2 原版用迭代器判界，此处 i + 2 < s.size() 与之等价：
   // 需要 s[i+1] 与 s[i+2] 都存在。

   // 手写 FeatureConfig::getDefaultPort 的等价逻辑（对照 FeatureConfig.cc:77-94）
   static int defaultPort(const std::string& proto)
   {
     if (proto == "http") return 80;
     if (proto == "https") return 443;
     if (proto == "ftp") return 21;
     if (proto == "sftp") return 22;
     return 0;
   }
   ```

2. 再写 `myParse`：调 `uri_split`，按 4.2.2 的装配规则（端口回填、dir/file 拆分、query 补 `?`、userinfo 解码、ipv6 标志）填一个自制结构体；然后写 `myConstruct`：按 4.2.3（5）的规则（percentEncode 凭证、IPv6 补方括号、默认端口省略、dir 补 `/`）重建 URI 字符串。
3. 主函数对一组 URL 逐一执行「parse → 打印九字段 → construct → 再 parse」：
   - `http://user%40x:p%40w@example.com:8080/a/b/file.tar.gz?q=1#frag`
   - `https://example.com`（默认端口省略往返）
   - `http://[2001:db8::1]:6881/dir/file`（IPv6）
   - `ftp://aria2@user@localhost/download/aria2-1.0.0.tar.bz2`（多 `@`）
   - `myproto://host/path`（应 parse 失败——白名单效应）
4. 断言 `construct(parse(u))` 再 parse 后与第一次的九字段完全一致（fragment 除外）。

**需要观察的现象**：往返后 `https://example.com:443/...` 的端口字段仍是 443，但 construct 输出省略了 `:443`；多 `@` 用例的 username 含字面 `@`、construct 时被重新编码为 `%40`；白名单外的协议在第一步就被拒绝。

**预期结果**：五条用例中前四条往返一致，第五条 `myproto` parse 失败。若某条不一致，对照 [src/uri.cc:L88-L209](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/uri.cc#L88-L209) 找出你漏掉的规则。完整输出待本地验证。

## 6. 本讲小结

- `util` 是无状态的自由函数库：字符串工具走「迭代器对 → 零拷贝区间」三层结构；数字解析统一走 `parseLong` 模板的「空串/溢出/垃圾尾串三查」骨架；`getRealSize` 只认 K/M 且用先除后乘防溢出。
- 文件名安全是一条流水线：`detectDirTraversal` 拦穿越，`fixTaintedBasename` 把 percent-decode 解出的 `/` 编回 `%2F`，`escapePath` 消灭控制字符，`createSafePath` 按是否合法 UTF-8 分流——恶意服务器/种子无法把文件写到 `-d` 目录之外。
- URL 解析分两层：纯 C 的 `uri_split` 状态机以「最后一个 `@` 为界」切出十个带偏移的字段且零分配；`uri::parse` 在其上补默认端口（http/https/ftp/sftp 白名单，其余协议拒绝）、dir/file 拆分与 userinfo 解码，`construct` 则做端口省略、IPv6 补括号的规范化逆变换。
- `File` 是只存路径字符串的值类型：一切查询经 `fillStat`→`a2stat` 单点收口，`mkdirs` 逐段创建并约定「已存在也返回 false」，`renameTo`+临时文件组合出 `saveAs` 的原子落盘——控制文件与 dht.dat 的可靠性都建立在这上面。
- `fmt` 是带 printf 格式检查的 `vsnprintf` 封装（2048 字节上限）；`percentEncode` 按 RFC 3986 unreserved 表编码、`percentDecode` 对残缺序列宽容；base32/base64 是标准字母表的位打包实现，输出与系统工具逐位一致。

## 7. 下一步学习建议

1. **下一讲 u9-l2（ValueBase 与解析器骨架）**：本讲看了两类「单趟状态机 + 偏移/区间输出」的结构（`uri_split`、`normalizePath`），u9-l2 的 `BencodeParser`/`JsonParser` 把同样的思想升级为「嵌套结构的解析状态机 + 回调」，可以直接对照着读。
2. **u9-l3（日志与异常体系）**：本讲多次出现 `DL_ABORT_EX` 与 `fmt` 的组合，u9-l3 讲清 `Exception` 家族如何携带错误码与堆栈，以及 `error_code` 退出码表的来历。
3. **回头重读两个调用点**，检验本讲效果：u4-l3 的 `CookieStorage::domainMatch`（用到 `util::isNumericHost` 判定「数字主机名不做后缀匹配」）；u6-l1 的 `bittorrent_helper`（对 tracker URL 与 url-list 逐个 `uri::parse`，并对其产物做 `detectDirTraversal` 防护）。
4. 想继续深挖平台抽象的读者，可以顺着 `File.cc` 的 `a2stat`/`a2mkdir` 打开 [src/a2io.h](https://github.com/aria2/aria2/blob/9e7273583f83e881e3ec067b523ba88724088d2f/src/a2io.h)，看 MinGW 分支如何把整族 POSIX 文件调用映射到 `_w*` 宽字符版本——这是 u9-l4 TLS 多后端矩阵之外另一张「平台兼容层」地图。

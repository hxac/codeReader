# 字符串与数值解析

## 1. 本讲目标

本讲紧接 [u2-l1 内存池](u2-l1-memory-pool.md)，进入 nginx 自研标准库的「文字层」：字符串与数值解析。

读完本讲你应当能够：

- 说清 `ngx_str_t` 为什么是「长度前缀字符串」，以及它和 C 字符串在 API 上的根本差别。
- 用 `ngx_string()` / `ngx_str_set()` / `ngx_str_null()` 三种方式正确初始化一个 `ngx_str_t`，并避开宏的陷阱。
- 读懂 `ngx_atoi` / `ngx_atofp` / `ngx_atosz` / `ngx_atoof` 系列函数，理解它们用 `cutoff`/`cutlim` 做溢出保护的经典写法。
- 用 `ngx_parse_size` / `ngx_parse_offset` / `ngx_parse_time` 解析配置里带单位的大小与时间（如 `64k`、`1g`、`1h30m`、`500ms`）。
- 理解 `ngx_parse_time` 的「单位必须从大到小排列」状态机，以及毫秒模式下为何不允许 `y`/`M`。
- 说出 `ngx_parse_url` 如何按 `unix:` / `[` / 普通三种前缀分发到不同子解析器。

## 2. 前置知识

本讲假设你已经掌握：

- **内存池**（[u2-l1](u2-l1-memory-pool.md)）：知道 `ngx_pool_t`、`ngx_palloc`、`ngx_pcalloc` 的存在。本讲的字符串复制函数 `ngx_pstrdup` 会在池上分配，但解析函数本身大多不需要池。
- **C 字符串的痛点**：标准 C 字符串以 `\0` 结尾，求长度要 `strlen` 遍历，且不能包含 `\0`。nginx 处理的是 HTTP 报文，报文里什么字节都可能出现，所以 nginx 几乎不用 C 字符串，而是自造一套。
- **整数溢出**：C 里 `int` 运算溢出是未定义行为。把用户配置里的数字字符串转成整数时，必须边转边检查是否越界，nginx 用了一个经典手法，本讲会拆解。

几个会在文中反复出现的术语：

- **长度前缀字符串（length-prefixed string）**：把「长度」和「数据指针」放一起，长度显式存储，不靠 `\0` 判断结尾。
- **slot 函数**：配置指令的通用解析回调，如 `ngx_conf_set_msec_slot`。本讲会看到它们最终都委托给本讲的解析函数。
- **状态机**：`ngx_parse_time` 用一个 `step` 枚举记录「上一次见到的是哪个单位」，从而强制单位顺序。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/core/ngx_string.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.h) | 定义 `ngx_str_t` 及一堆字符串宏与函数声明 |
| [src/core/ngx_string.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.c) | 字符串操作实现 + `ngx_atoi` / `ngx_atofp` / `ngx_atosz` / `ngx_atoof` / `ngx_hextoi` 等数值解析 |
| [src/core/ngx_parse.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_parse.c) | `ngx_parse_size` / `ngx_parse_offset` / `ngx_parse_time`（带单位的大小与时间解析） |
| [src/core/ngx_parse_time.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_parse_time.c) | `ngx_parse_http_time`（解析 HTTP 日期头，如 `Tue, 10 Nov 2002 23:50:13`） |
| [src/core/ngx_inet.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.c) | `ngx_parse_url`（解析监听/上游地址，含 unix 域与 IPv6 分发） |
| [src/core/ngx_inet.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.h) | `ngx_url_t` 结构定义 |
| [src/core/ngx_conf_file.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c) | slot 函数，是把配置文本接到本讲解析函数的桥梁 |

> 提示：文件名容易混淆——`ngx_parse.c` 里是**时长**解析 `ngx_parse_time`（如 `1h30m`）；`ngx_parse_time.c` 里是 **HTTP 日期**解析 `ngx_parse_http_time`（如 `Tue, 10 Nov ...`）。两者完全不同，本讲都会涉及。

## 4. 核心概念与源码讲解

### 4.1 ngx_str_t：长度前缀字符串

#### 4.1.1 概念说明

nginx 几乎所有「一段文字」（请求行、头部名/值、配置参数、变量值）都用 `ngx_str_t` 表示。它的定义极其简单：

```c
typedef struct {
    size_t      len;
    u_char     *data;
} ngx_str_t;
```

- `len`：这段文字的字节长度，**显式存储**，不靠 `\0`。
- `data`：指向文字首字节，**不保证以 `\0` 结尾**。

为什么不用 C 字符串？三个理由：

1. HTTP 报文里任何字节都可能出现，包括 `\0`，靠 `\0` 判结尾会截断数据。
2. 求长度是 O(1)（直接读 `len`），而 `strlen` 是 O(n)。nginx 对同一字符串可能反复用长度，省下来很可观。
3. nginx 经常持有「某个 buffer 中间的一段」，不需要也不应该拷贝出一份带 `\0` 的副本，`data`+`len` 直接切片即可。

代价是：要把 `ngx_str_t` 当 C 字符串传给 libc（如 `printf("%s")`）时，必须保证 `data` 指向的内容以 `\0` 结尾，否则会越界读。很多 nginx 代码在需要时会显式补一个 `\0`。

#### 4.1.2 核心流程

初始化一个 `ngx_str_t` 有三种常见方式，对应不同来源：

1. **编译期字面量**：用 `ngx_string("...")` 宏，在声明时直接初始化，`len` 由 `sizeof` 算出。
2. **运行期赋值**：用 `ngx_str_set(&s, "...")` 宏，给已存在的变量赋一个字面量。
3. **置空**：用 `ngx_str_null(&s)` 宏，把 `len` 置 0、`data` 置 `NULL`，表示「没有值」。

字符串比较、拷贝、大小写转换则用一组函数/宏：`ngx_strcmp`/`ngx_strncmp`（其实就是 libc 的 `strcmp`/`strncmp`）、`ngx_strlow`（小写化）、`ngx_cpystrn`（带长度限制的拷贝，遇到 `\0` 提前停止）、`ngx_pstrdup`（在内存池上复制一份）。

#### 4.1.3 源码精读

**结构定义** —— [src/core/ngx_string.h:16-19](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.h#L16-L19) 定义了 `ngx_str_t`：只有 `len` 和 `data` 两个字段，是 nginx 里最基础的结构体之一。

**三个初始化宏** —— [src/core/ngx_string.h:40-44](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.h#L40-L44)：

```c
#define ngx_string(str)     { sizeof(str) - 1, (u_char *) str }
#define ngx_null_string     { 0, NULL }
#define ngx_str_set(str, text)  \
    (str)->len = sizeof(text) - 1; (str)->data = (u_char *) text
#define ngx_str_null(str)   (str)->len = 0; (str)->data = NULL
```

`ngx_string("abc")` 展开为 `{ 3, (u_char*)"abc" }`，`sizeof("abc")` 是 4（含 `\0`），减 1 得到长度 3。注意 `ngx_str_set` 和 `ngx_str_null` 是**两条语句**，没有 `do{}while(0)` 包裹——所以不能写成 `if (cond) ngx_str_set(s, "x"); else ...;`，那会被解析成 `if (cond) { s->len=...; } s->data=...; else ...;` 导致 `else` 悬空报错。这是 nginx 宏的一个真实坑点。

**大小写转换** —— [src/core/ngx_string.h:47-48](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.h#L47-L48) 的 `ngx_tolower`/`ngx_toupper` 用位运算而不是 `if`，对 ASCII 字母快一拍：`c | 0x20` 转小写、`c & ~0x20` 转大写。批量转换用 [src/core/ngx_string.c:22-31](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.c#L22-L31) 的 `ngx_strlow`，逐字节套 `ngx_tolower`。

**libc 包装** —— [src/core/ngx_string.h:53-61](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.h#L53-L61) 把 `ngx_strcmp`/`ngx_strncmp`/`ngx_strstr`/`ngx_strlen` 直接 `#define` 成 libc 同名函数。可见 nginx 并不排斥 libc，只是在「需要长度显式化」的场景才用 `ngx_str_t`。

**带长拷贝** —— [src/core/ngx_string.c:50-60](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.c#L50-L60) 的 `ngx_cpystrn(dst, src, n)` 拷贝最多 `n-1` 个字符并补 `\0`，遇到 `\0` 提前停——它是把 `ngx_str_t` 落到 C 字符串的桥梁（例如前面 `ngx_parse_unix_domain_url` 里拷贝 `sun_path`）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是用三种方式初始化 `ngx_str_t` 并观察字段值。

1. 实践目标：理解 `ngx_string` / `ngx_str_set` / `ngx_str_null` 的展开结果。
2. 操作步骤：
   - 打开 [src/core/ngx_string.h:40-44](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.h#L40-L44)。
   - 在脑中（或用 `gcc -E` 预处理）展开下面这段**示例代码**（非项目原有代码）：
     ```c
     ngx_str_t  a = ngx_string("hello");      /* a.len=5, a.data="hello"  */
     ngx_str_t  b = ngx_null_string;          /* b.len=0, b.data=NULL     */
     ngx_str_t  c;
     ngx_str_set(&c, "GET");                  /* c.len=3, c.data="GET"    */
     ngx_str_null(&c);                        /* c.len=0, c.data=NULL     */
     ```
3. 需要观察的现象：`ngx_string("hello")` 的 `len` 是 5 而非 6（`sizeof` 含 `\0`，减 1）；`ngx_null_string` 与 `ngx_str_null` 产物相同。
4. 预期结果：`a.len==5`、`b.len==0 && b.data==NULL`、`ngx_str_set` 后 `c.len==3`。
5. 若想本地确认，可写一个只 `#include <ngx_config.h>` 与 `#include <ngx_core.h>` 的小程序打印上述字段；编译方式见第 5 节综合实践（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ngx_string("abc")` 里要写 `sizeof(str) - 1`，而不是直接写 `3`？

> **答案**：`"abc"` 是字面量，`sizeof("abc")` 含末尾 `\0` 等于 4；`ngx_str_t.len` 只计可见字节，故减 1 得 3。用 `sizeof` 让宏对任意字面量自适应，避免手数错。

**练习 2**：下面代码有什么隐患？
```c
if (ok) ngx_str_set(&s, "on");
else    ngx_str_null(&s);
```

> **答案**：`ngx_str_set` 展开成两条语句且无 `do{}while(0)`，`if` 只吞掉第一条 `(s)->len=...`，第二条 `(s)->data=...` 脱离了 `if`，`else` 会与外层 `if` 配对导致悬空 else / 语义错误。应改为 `{ ngx_str_set(&s, "on"); }` 加花括号，或用 `ngx_string()` 在声明期初始化。

### 4.2 ngx_atoi / ngx_atofp：整数与定点数解析

#### 4.2.1 概念说明

把配置里的数字字符串（端口 `8080`、权重 `5`、限流 `100`）转成整数，是配置解析的高频操作。标准库 `atoi` 有两个问题：① 遇到非数字就停，但「停在哪」要靠返回值猜测，错误处理弱；② 不做溢出检查，`atoi("99999999999999")` 行为未定义。

nginx 自造 `ngx_atoi(line, n)` 解决这两点：

- **长度显式**：调用方传 `n`（要解析的字节数），函数只看这 `n` 个字节，不依赖 `\0`。这正好配合 `ngx_str_t` 的 `len`。
- **严格数字**：这 `n` 个字节里只要有一个不是 `'0'`~`'9'`，立刻返回 `NGX_ERROR`。
- **溢出保护**：边乘边加边检查，越界返回 `NGX_ERROR`。

围绕它还有一族变体，差别只在「返回类型」和「最大值」，对应不同用途：

| 函数 | 返回类型 | 上界常量 | 用途 |
|---|---|---|---|
| `ngx_atoi` | `ngx_int_t` | `NGX_MAX_INT_T_VALUE` | 通用整数（端口、数量） |
| `ngx_atosz` | `ssize_t` | `NGX_MAX_SIZE_T_VALUE` | 大小类（字节数） |
| `ngx_atoof` | `off_t` | `NGX_MAX_OFF_T_VALUE` | 文件偏移 |
| `ngx_atotm` | `time_t` | `NGX_MAX_TIME_T_VALUE` | 时间秒数 |
| `ngx_atofp` | `ngx_int_t` | 同 `ngx_atoi` | 定点数（带小数点） |
| `ngx_hextoi` | `ngx_int_t` | 同 `ngx_atoi` | 十六进制（如颜色 `#ff0000`） |

`ngx_atofp("10.5", 4, 2)` 返回 `1050`——它把 `10.5` 当作「2 位小数」的定点数，乘以 100 存成整数 `1050`，避免浮点误差。

#### 4.2.2 核心流程

`ngx_atoi` 的算法是教科书式的「溢出安全字符串转整数」。设最大值为 `MAX`，令：

\[
\text{cutoff} = \lfloor \text{MAX} / 10 \rfloor, \quad \text{cutlim} = \text{MAX} \bmod 10
\]

即 `MAX = cutoff * 10 + cutlim`。对每个数字 `d`，在执行 `value = value*10 + d` **之前**判断：

\[
\text{若 } \text{value} \ge \text{cutoff} \text{ 且 } (\text{value} > \text{cutoff} \ \text{或}\  d > \text{cutlim}) \text{，则溢出。}
\]

推理：

- `value < cutoff`：`value*10 + d < cutoff*10 ≤ MAX`，安全。
- `value == cutoff`：`value*10 + d = MAX - cutlim + d`，仅当 `d ≤ cutlim` 安全。
- `value > cutoff`：`value*10 ≥ cutoff*10 + 10 > MAX`（因 `cutlim < 10`），必溢出。

这样在乘加之前就拦住越界，避免依赖溢出后的未定义行为。`ngx_atofp` 在此基础上多处理一个 `.` 和「小数位数补 0」。

#### 4.2.3 源码精读

**ngx_atoi** —— [src/core/ngx_string.c:966-991](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.c#L966-L991)：

```c
ngx_int_t
ngx_atoi(u_char *line, size_t n)
{
    ngx_int_t  value, cutoff, cutlim;

    if (n == 0) {
        return NGX_ERROR;
    }

    cutoff = NGX_MAX_INT_T_VALUE / 10;
    cutlim = NGX_MAX_INT_T_VALUE % 10;

    for (value = 0; n--; line++) {
        if (*line < '0' || *line > '9') {
            return NGX_ERROR;
        }

        if (value >= cutoff && (value > cutoff || *line - '0' > cutlim)) {
            return NGX_ERROR;
        }

        value = value * 10 + (*line - '0');
    }

    return value;
}
```

注意三点：① `n == 0` 直接返回错误（空串不算 0）；② 非数字字节返回 `NGX_ERROR`，所以 `ngx_atoi("12a", 3)` 是错误而非 `12`；③ 溢出判断正是 4.2.2 推导的那条式子。

**ngx_atofp** —— [src/core/ngx_string.c:996-1047](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.c#L996-L1047)。函数上方 [注释 994 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.c#L994) 给了例子 `ngx_atofp("10.5", 4, 2) returns 1050`。它的要点：遇到 `.` 只允许出现一次（`dot` 标志），第二个 `.` 报错；`point` 记录还应读入多少位小数，读完后若位数不够，在末尾用 `while (point--) value *= 10;` 补 0（如 `"10.5"` 期望 2 位小数，已读 1 位，补一个 0 → `105` → `1050`）。

**同族变体** —— [ngx_atosz:1050-1075](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.c#L1050-L1075) 与 [ngx_atoof:1078-1103](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.c#L1078-L1103) 与 `ngx_atoi` 几乎逐字相同，只是 `value`/`cutoff`/`cutlim` 的类型和上界常量换成 `ssize_t`/`off_t` 对应的最大值。

**被谁调用** —— [src/core/ngx_conf_file.c:1183-1186](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1183-L1186) 的 `ngx_conf_set_num_slot` 把指令参数交给 `ngx_atoi`，转换失败就报 `"invalid number"`。这就是配置里写 `worker_processes 4;` 时数字被吃掉的地方。

#### 4.2.4 代码实践

**源码阅读型实践**：用 4.2.2 的公式手算 `ngx_atoi` 的返回值。

1. 实践目标：验证「长度显式 + 严格数字 + 溢出保护」三条性质。
2. 操作步骤：对下面每组调用，先按源码推导返回值，再对照答案。
   - `ngx_atoi((u_char*)"8080", 4)`
   - `ngx_atoi((u_char*)"8080abc", 4)`（只看前 4 字节）
   - `ngx_atoi((u_char*)"8080abc", 7)`（含 `a`）
   - `ngx_atoi((u_char*)"", 0)`
   - `ngx_atofp((u_char*)"10.5", 4, 2)`
3. 需要观察的现象：同一个缓冲区 `"8080abc"`，传 `n=4` 成功返回 `8080`，传 `n=7` 因第 5 字节 `'a'` 非数字而返回 `NGX_ERROR`。这正说明 `ngx_atoi` 是**长度限定**的，`n` 就是调用方根据 token 边界算出的字节长度（字节偏移之差）。
4. 预期结果：`8080` / `8080` / `NGX_ERROR(-1)` / `NGX_ERROR(-1)` / `1050`。
5. 待本地验证：可用第 5 节的探针程序实际编译运行确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ngx_atoi` 不像 `atoi` 那样自己扫到非数字就停，而要调用方传 `n`？

> **答案**：nginx 的字符串是 `ngx_str_t`（长度前缀），不保证以 `\0` 结尾，也没有可靠的「结束符」可扫。由调用方传 `n`（通常就是 `str.len`，或两个指针之差即字节偏移之差），函数精确处理这段区间，既安全又无需拷贝出 C 字符串。

**练习 2**：`ngx_atofp("1.25", 4, 2)` 返回多少？`ngx_atofp("1.2", 3, 2)` 又返回多少？

> **答案**：`"1.25"` 期望 2 位小数，读完整数 `1`、跳过 `.`、读小数 `25`，得 `125`，无需补 0 → 返回 `125`（表示 1.25）。`"1.2"` 期望 2 位小数但只读到 `2` 一位，得 `12`，再 `while(point--) value*=10` 补一位 0 → `120`（表示 1.20）。

### 4.3 ngx_parse_size / ngx_parse_offset / ngx_parse_time：带单位的大小与时间

#### 4.3.1 概念说明

配置里大量出现「数字 + 单位」：`client_max_body_size 64m;`、`proxy_cache_path ... max_size=1g;`、`keepalive_timeout 1h30m;`、`lingering_time 500ms;`。这些不能直接 `ngx_atoi`，因为末尾有字母单位。nginx 用三个函数处理：

- `ngx_parse_size`：解析 `K`/`M`（大小写不敏感），返回 `ssize_t` 字节，用于 `size` 类指令。
- `ngx_parse_offset`：解析 `K`/`M`/`G`，返回 `off_t`，用于文件偏移/缓存大小类指令（上界更大）。
- `ngx_parse_time`：解析时间 `y`/`M`/`w`/`d`/`h`/`m`/`s`/`ms`，用第二个参数 `is_sec` 控制返回秒还是毫秒。

其中 `ngx_parse_time` 最复杂，是一个**带顺序约束的状态机**：单位必须从大到小出现（`1h30m` 合法，`30m1h` 非法），且毫秒模式下不允许 `y`/`M`。

#### 4.3.2 核心流程

**`ngx_parse_size` / `ngx_parse_offset`** 流程很直白：

1. 取字符串最后一个字符作为 `unit`。
2. 按单位查表得 `scale`（`K`→1024，`M`→1024²，`G`→1024³）与对应上界 `max`；无单位则 `scale=1`。
3. 把去掉单位后的数字部分交给 `ngx_atosz` / `ngx_atoof` 解析。
4. 检查不超 `max`，乘以 `scale` 返回。

**`ngx_parse_time`** 是状态机。它维护一个枚举 `step`，记录「当前已解析到的最小单位」：

```
st_start → st_year → st_month → st_week → st_day → st_hour → st_min → st_sec → st_msec
```

每遇到一个单位字符，先检查 `step` 是否已越过该单位（即是否「比当前单位更小的单位已经出现过」），是则报错——这就强制了从大到小的顺序。然后把当前数字 `value` 乘以该单位的 `scale` 累加到 `total`。

`is_sec` 参数是关键开关：

- `is_sec = 1`：返回**秒**。`step` 初值 `st_start`（允许从 `y` 开始）；`ms` 非法。
- `is_sec = 0`：返回**毫秒**。`step` 初值 `st_month`（所以 `y`/`M` 会被拒，详见 4.3.4）；每个非 `ms` 单位的 `scale` 乘 1000；`ms` 合法且 `scale=1`。

为何毫秒模式拒绝 `y`/`M`？因为 1 年 ≈ 3.15×10¹⁰ ms、1 月 ≈ 2.59×10⁹ ms，在 32 位 `ngx_int_t`（上界约 2.1×10⁹）下会溢出。nginx 用「初始 `step` 提前到 `st_month`」一刀切掉这两个单位，比到处加判断更干净。

#### 4.3.3 源码精读

**ngx_parse_size** —— [src/core/ngx_parse.c:12-55](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_parse.c#L12-L55)：取末字符 `unit`，`switch` 查 `scale`/`max`，调 `ngx_atosz` 解析剩余数字，乘 `scale` 返回。

**ngx_parse_offset** —— [src/core/ngx_parse.c:58-108](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_parse.c#L58-L108)：结构同上，多了 `G`（1024³），改用 `ngx_atoof` 与 `NGX_MAX_OFF_T_VALUE`。

**ngx_parse_time** —— [src/core/ngx_parse.c:111-283](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_parse.c#L111-L283)。关键片段：

[状态枚举 118-129 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_parse.c#L118-L129) 定义了 `step` 的取值序列；[136 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_parse.c#L136) `step = is_sec ? st_start : st_month;` 设初值——这就是毫秒模式拒绝 `y`/`M` 的开关。

[毫秒分支 200-218 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_parse.c#L200-L218) 处理 `m`：先看下一个字符是不是 `s`，是则走 `ms` 分支（且 `is_sec` 时报错），否则当分钟 `m` 处理：

```c
case 'm':
    if (p < last && *p == 's') {
        if (is_sec || step >= st_msec) {
            return NGX_ERROR;
        }
        p++;
        step = st_msec;
        max = NGX_MAX_INT_T_VALUE;
        scale = 1;
        break;
    }
    ...
```

[242-245 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_parse.c#L242-L245) 是毫秒模式的统一放大：

```c
if (step != st_msec && !is_sec) {
    scale *= 1000;
    max /= 1000;
}
```

即「非 ms 的单位，在毫秒模式下 scale 乘 1000」。最后 [270-282 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_parse.c#L270-L282) 收尾：若结尾还有未跟单位的数字（纯数字串），在毫秒模式下乘 1000 补成毫秒，再累加到 `total` 返回。

**slot 桥梁** —— [ngx_conf_set_msec_slot:1276-1279](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1276-L1279) 调 `ngx_parse_time(&value[1], 0)`（毫秒），[ngx_conf_set_sec_slot:1307-1310](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_conf_file.c#L1307-L1310) 调 `ngx_parse_time(&value[1], 1)`（秒）。失败统一报 `"invalid value"`。

#### 4.3.4 代码实践

这是一个**可运行实践**，用真实 nginx 二进制 + `nginx -t` 验证 `ngx_parse_time`（毫秒模式）的行为。`client_header_timeout` 指令正是 msec slot —— 见 [src/http/ngx_http_core_module.c:234-236](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L234-L236)。

1. 实践目标：通过 `nginx -t` 接受/拒绝配置，反推 `ngx_parse_time` 在 `is_sec=0` 下对各种时间串的判定。
2. 操作步骤：
   - 准备一个最小 `nginx.conf`（假设放在 `/tmp/probe/nginx.conf`）：
     ```nginx
     events { }
     http {
         client_header_timeout 1h30m;      # 依次替换为下列各值
         server { listen 127.0.0.1:18080; }
     }
     ```
   - 用 u1-l2 编译出的二进制测试：`objs/nginx -p /tmp/probe/ -t`（`-p` 给前缀，`-t` 只测配置不启动）。
   - 依次把 `client_header_timeout` 的值换成下表各项，每次跑 `nginx -t`。
3. 需要观察的现象：合法值打印 `the configuration file ... syntax is ok`；非法值打印 `invalid value`。
4. 预期结果（基于源码推导，请本地运行确认）：

   | 值 | 预期 | 原因 |
   |---|---|---|
   | `1h30m` | ok | 单位从大到小，合法 |
   | `500ms` | ok | 毫秒模式允许 `ms` |
   | `30m1h` | invalid | `h` 出现在 `m` 之后，顺序错 |
   | `1y` | invalid | 毫秒模式 `step` 初值 `st_month`，`y` 被拒 |
   | `1M` | invalid | 毫秒模式 `step` 初值即 `st_month`，`M` 被拒 |
   | `60` | ok | 纯数字，毫秒模式按 60ms 处理 |

5. 待本地验证：`-p` 前缀目录需存在且 `nginx.conf` 路径正确；若端口冲突可任意改 `listen` 端口，因为 `-t` 不真正 bind。

#### 4.3.5 小练习与答案

**练习 1**：`ngx_parse_time("1h30m", 0)` 和 `ngx_parse_time("1h30m", 1)` 分别返回多少？

> **答案**：前者毫秒模式，`1h`=3600×1000=3,600,000，`30m`=30×60×1000=1,800,000，合计 `5,400,000`（ms）。后者秒模式，`1h`=3600，`30m`=1800，合计 `5400`（s）。即同一个串，`is_sec` 决定返回单位差 1000 倍。

**练习 2**：为什么 `ngx_parse_time("1y500ms", 0)` 会失败，而 `ngx_parse_time("1y", 1)` 成功？

> **答案**：`is_sec=0`（毫秒模式）下 `step` 初值为 `st_month`，`y` 分支判断 `step > st_start` 为真直接返回错误，所以凡含 `y` 的毫秒串都非法。`is_sec=1`（秒模式）下 `step` 初值为 `st_start`，`y` 合法——但秒模式又禁止 `ms`，所以 `1y500ms` 在秒模式同样非法。两模式各自切掉了一头：毫秒切 `y`/`M`，秒切 `ms`。

**练习 3**：`ngx_parse_offset("1g")` 返回多少？为什么不用 `ngx_parse_size`？

> **答案**：返回 `1024*1024*1024 = 1073741824`（字节）。`ngx_parse_size` 返回 `ssize_t` 且只认 `K`/`M`、上界是 `NGX_MAX_SIZE_T_VALUE`；`1g` 数值大、常用于缓存/文件偏移，需要 `off_t` 与 `G` 单位及更大的 `NGX_MAX_OFF_T_VALUE` 上界，故用 `ngx_parse_offset`。

### 4.4 ngx_parse_url：监听地址与 URL 解析

#### 4.4.1 概念说明

配置里的 `listen 127.0.0.1:8080;`、`proxy_pass http://backend;`、`upstream b { server unix:/tmp/x.sock; }` 都要把一个「地址串」解析成可用的 socket 地址。这事比解析数字复杂得多：地址可能是 IPv4、IPv6（`[::1]:8080`）、unix 域套接字（`unix:/path`），可能带端口、带 URI、带通配。

nginx 把结果填进 `ngx_url_t` 结构，由 `ngx_parse_url` 统一入口、按前缀分发到三个子解析器。注意它**需要内存池**（要在池上分配 `addrs` 数组等），这点和前面几个无池解析函数不同。

#### 4.4.2 核心流程

`ngx_parse_url` 的分发逻辑非常简洁：

1. 看 `url` 是否以 `unix:` 开头 → 调 `ngx_parse_unix_domain_url`。
2. 否则看首字符是否为 `[` → 调 `ngx_parse_inet6_url`（IPv6）。
3. 否则 → 调 `ngx_parse_inet_url`（IPv4 / 主机名 / 通配）。

子解析器把结果写回 `ngx_url_t` 的字段：`host`、`port`、`port_text`、`uri`、`family`、`sockaddr`/`socklen`、`addrs`/`naddrs`，以及出错时填 `err` 字符串。调用方事先填好 `url`（待解析串）、`default_port`（缺省端口）、`listen`（是否用于监听）、`no_resolve`（是否跳过 DNS 解析）等入参。

#### 4.4.3 源码精读

**ngx_url_t 结构** —— [src/core/ngx_inet.h:81-106](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.h#L81-L106) 定义了入参/出参合一的字段：`url`/`host`/`port_text`/`uri` 是字符串视图，`port`/`default_port`/`family` 是数值，`listen`/`uri_part`/`no_resolve`/`no_port`/`wildcard` 是位标志，`sockaddr`/`socklen`/`addrs`/`naddrs` 是解析结果，`err` 是错误说明。

**三分发** —— [src/core/ngx_inet.c:688-705](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.c#L688-L705)：

```c
ngx_parse_url(ngx_pool_t *pool, ngx_url_t *u)
{
    u_char  *p;
    size_t   len;

    p = u->url.data;
    len = u->url.len;

    if (len >= 5 && ngx_strncasecmp(p, (u_char *) "unix:", 5) == 0) {
        return ngx_parse_unix_domain_url(pool, u);
    }

    if (len && p[0] == '[') {
        return ngx_parse_inet6_url(pool, u);
    }

    return ngx_parse_inet_url(pool, u);
}
```

注意它用 `ngx_strncasecmp` 比较 `unix:` 前缀（大小写不敏感），所以 `UNIX:/tmp/x` 也能识别。`ngx_parse_unix_domain_url` 内部（[709 行起](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.c#L709)）用 `ngx_strlchr` 找 `:` 切出 URI 部分，用 `ngx_cpystrn` 把路径拷进 `sun_path`，最后在池上分配 `ngx_addr_t` 填好 `sockaddr`——这里就能看到 4.1 学的 `ngx_cpystrn` 和 u2-l1 学的 `ngx_pcalloc` 协同工作。

> 说明：`ngx_parse_url` 与 `ngx_parse_addr`、`ngx_cidr` 等 IP/CIDR 解析同属 `ngx_inet.c`，后者在 [u3-l4 指令类型与地址族](u3-l4-directives-and-inet.md) 详讲。本讲只看 `ngx_parse_url` 的分发主入口。

#### 4.4.4 代码实践

**源码阅读型实践**：追踪三种地址串分别走哪个子解析器、各自填了哪些 `ngx_url_t` 字段。

1. 实践目标：把 `ngx_parse_url` 的分发与结果落到具体字段上。
2. 操作步骤：对照 [ngx_parse_url:688-705](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.c#L688-L705) 与 [ngx_url_t:81-106](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_inet.h#L81-L106)，按下表填写「走哪个子解析器 / 关键产出字段」。

   | 输入 `url` | 子解析器 | 关键产出 |
   |---|---|---|
   | `127.0.0.1:8080` | ? | ? |
   | `[::1]:8080` | ? | ? |
   | `unix:/tmp/x.sock` | ? | ? |
   | `0.0.0.0` | ? | ?（`wildcard` 标志？） |
3. 需要观察的现象：`unix:` 前缀优先级最高；`[` 次之；其余走 inet。`unix:` 分支会设置 `family=AF_UNIX` 并填 `sockaddr_un`。
4. 预期结果：`127.0.0.1:8080` → `ngx_parse_inet_url`，产出 `host=127.0.0.1`、`port=8080`、`family=AF_INET`、`addrs[0]` 含 `sockaddr_in`；`[::1]:8080` → `ngx_parse_inet6_url`，`family=AF_INET6`；`unix:/tmp/x.sock` → `ngx_parse_unix_domain_url`，`family=AF_UNIX`、`addrs[0].sockaddr` 为 `sockaddr_un`；`0.0.0.0` → `ngx_parse_inet_url` 且 `wildcard=1`。
5. 待本地验证：`ngx_parse_inet_url` 内部对通配与 DNS 的分支较细，`wildcard` 的精确置位条件建议阅读该函数确认。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ngx_parse_url` 用 `ngx_strncasecmp` 比较 `unix:`，而首字符 `[` 却用普通比较？

> **答案**：`unix:` 是关键字，地址族关键字大小写不敏感更友好（用户可能写 `UNIX:`），故用 `ngx_strncasecmp`。而 `[` 是 IPv6 地址的语法分隔符（RFC 规定 IPv6 字面量用方括号包裹），不存在大小写问题，直接比 `p[0] == '['` 即可，也更快。

**练习 2**：`ngx_parse_url` 为什么要传 `ngx_pool_t *pool`，而 `ngx_parse_time` 不要？

> **答案**：`ngx_parse_url` 要把解析出的 `addrs` 数组、`sockaddr` 结构等「结果对象」分配在堆上并长期持有，用内存池分配（`ngx_pcalloc`）可随池统一回收、且避免碎片。`ngx_parse_time` 只返回一个 `ngx_int_t` 标量，无需分配内存，故不需要池。

## 5. 综合实践

把本讲三个最小模块串起来：用 `ngx_str_t` 构造输入串，调 `ngx_parse_time` 解析时长、`ngx_atoi` 解析端口，并验证「长度限定」行为。

### 5.1 实践目标

- 用 `ngx_string()` 初始化 `ngx_str_t`。
- 调 `ngx_parse_time("1h30m", 0)` 与 `ngx_parse_time("500ms", 0)`，验证返回的毫秒数。
- 调 `ngx_atoi` 解析端口串，验证它只消费调用方给的 `n` 个字节（即「字节偏移/长度」边界）。

### 5.2 操作步骤

写一个**示例代码**（非项目原有文件）`parse_probe.c`：

```c
#include <ngx_config.h>
#include <ngx_core.h>
#include <stdio.h>

int main(void)
{
    ngx_str_t  s;
    ngx_int_t  v;

    /* ngx_parse_time：毫秒模式（is_sec=0） */
    s = ngx_string("1h30m");
    v = ngx_parse_time(&s, 0);
    printf("parse_time(\"1h30m\", 0) = %lld  (期望 5400000)\n", (long long) v);

    s = ngx_string("500ms");
    v = ngx_parse_time(&s, 0);
    printf("parse_time(\"500ms\", 0) = %lld  (期望 500)\n", (long long) v);

    /* 对照：秒模式（is_sec=1）下 500ms 应失败 */
    s = ngx_string("500ms");
    v = ngx_parse_time(&s, 1);
    printf("parse_time(\"500ms\", 1) = %lld  (期望 -1, NGX_ERROR)\n", (long long) v);

    /* ngx_atoi：长度限定，n 是字节偏移之差 */
    u_char *buf = (u_char *) "8080abc";
    v = ngx_atoi(buf, 4);           /* 只看前 4 字节 "8080" */
    printf("atoi(\"8080abc\", 4) = %lld  (期望 8080)\n", (long long) v);

    v = ngx_atoi(buf, 7);           /* 含 'a'，应失败 */
    printf("atoi(\"8080abc\", 7) = %lld  (期望 -1)\n", (long long) v);

    return 0;
}
```

编译（需先按 [u1-l2](u1-l2-build-and-run.md) 跑过 `./configure && make`，使 `objs/` 下有编译产物）：

```sh
gcc -o parse_probe parse_probe.c \
    -I objs -I src/core -I src/event -I src/os/unix \
    objs/src/core/ngx_parse.o objs/src/core/ngx_string.o \
    objs/src/core/ngx_palloc.o objs/src/core/ngx_alloc.o \
    objs/src/core/ngx_array.o objs/src/core/ngx_log.o \
    objs/src/core/ngx_buf.o objs/src/core/ngx_pool.o \
    objs/src/os/unix/*.o \
    -lpthread -ldl -lm
```

> 待本地验证：`ngx_string.o`/`ngx_parse.o` 中的其它函数可能引用额外符号，若链接报 `undefined reference`，按报错把对应的 `objs/src/core/*.o`（去掉 `nginx.o`，它含 `main`）补进命令即可。`ngx_parse_time` 与 `ngx_atoi` 本身不依赖内存池与日志，符号闭包很小。

运行：`./parse_probe`

### 5.3 需要观察的现象

- `1h30m` 在毫秒模式下得到 `5400000`（= 5400 秒 = 1.5 小时）。
- `500ms` 在毫秒模式下得到 `500`；在秒模式下得到 `-1`。
- 同一缓冲区 `"8080abc"`，传 `n=4` 得 `8080`，传 `n=7` 得 `-1`——证明 `ngx_atoi` 严格按调用方给的字节长度工作，`n` 就是 token 边界的字节偏移之差。

### 5.4 预期结果

```
parse_time("1h30m", 0) = 5400000  (期望 5400000)
parse_time("500ms", 0) = 500  (期望 500)
parse_time("500ms", 1) = -1  (期望 -1, NGX_ERROR)
atoi("8080abc", 4) = 8080  (期望 8080)
atoi("8080abc", 7) = -1  (期望 -1)
```

> 以上数值是依据源码逐行推导的预期；是否实际如此，待本地验证（尤其是链接命令，可能需按本地 configure 选项微调 `.o` 列表）。

## 6. 本讲小结

- `ngx_str_t = {len, data}` 是长度前缀字符串，不靠 `\0`，长度 O(1)，能容纳任意字节；初始化用 `ngx_string()`（声明期）、`ngx_str_set()`/`ngx_str_null()`（运行期，注意是无 `do{}while(0)` 的双语句宏）。
- `ngx_atoi(line, n)` 是「长度限定 + 严格数字 + 溢出保护」的整数解析；`ngx_atosz`/`ngx_atoof`/`ngx_atotm` 只是换返回类型与上界；`ngx_atofp` 处理定点数，`ngx_hextoi` 处理十六进制。
- 溢出保护用 `cutoff=MAX/10`、`cutlim=MAX%10`，在乘加之前判断 `value >= cutoff && (value > cutoff || d > cutlim)`，避免依赖未定义行为。
- `ngx_parse_size`（K/M）、`ngx_parse_offset`（K/M/G）看末字符定单位再放大；`ngx_parse_time` 是带顺序约束的状态机，单位必须从大到小，`is_sec` 决定返回秒还是毫秒。
- 毫秒模式（`is_sec=0`）下 `step` 初值为 `st_month`，故 `y`/`M` 被禁（防 32 位溢出）；秒模式（`is_sec=1`）下 `ms` 被禁。slot 函数 `ngx_conf_set_msec_slot`/`set_sec_slot` 是配置文本到这两个模式的桥梁。
- `ngx_parse_url` 按 `unix:` / `[` / 其它三分发到 unix/inet6/inet 子解析器，结果填入 `ngx_url_t`；它需要内存池，因为要在池上分配 `addrs` 等结果对象。
- `ngx_parse_time.c` 里的 `ngx_parse_http_time` 是另一回事——解析 HTTP 日期头（RFC822/850/ISOC），别和时长解析 `ngx_parse_time` 混淆。

## 7. 下一步学习建议

- 下一讲 [u2-l3 容器数据结构](u2-l3-containers.md) 会用到本讲的 `ngx_str_t`：`ngx_hash` 的键、`ngx_str_node_t`（见 [ngx_string.h:218-221](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_string.h#L218-L221)）都把 `ngx_str_t` 当成员，红黑树按字符串键查找。
- 想看本讲解析函数的「真实调用现场」，可跳到 [u3-l1 配置文件解析器](u3-l1-conf-parse.md) 与 [u3-l4 指令类型与地址族](u3-l4-directives-and-inet.md)：前者讲 `ngx_conf_handler` 如何把一条指令派发给 slot 函数，后者展开 `ngx_parse_url`/`ngx_parse_addr`/`ngx_cidr` 在 `listen`、`allow`/`deny` 中的具体应用。
- 对 HTTP 日期解析感兴趣的读者，可直接精读 [ngx_parse_http_time:14-277](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_parse_time.c#L14-L277)，它用高斯公式把年月日折算成自 1970-01-01 的秒数，是本讲未展开的彩蛋。
- 建议同步阅读 `src/core/ngx_string.c` 中 `ngx_sprintf`/`ngx_snprintf` 一节（格式化输出，与解析对称），为日后读日志格式化与变量求值打基础。

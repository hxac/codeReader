# KeyEvent 与 KeyTable：按键的内部表示

## 1. 本讲目标

上一篇我们用 `rime_api_console` 端到端走了一遍输入流程，看到前端把按键喂给 `process_key`。但「一个按键」在 librime 内部到底长什么样？本讲就拆开这第一个运行时对象。学完后你应当能够：

- 说清 `KeyEvent` 为什么用 **两个 `int`**（`keycode` + `modifier`）来表示一次按键；
- 看懂 `kShiftMask` / `kControlMask` / `kReleaseMask` 等**位掩码**，并能做按位或/按位与的运算；
- 解释按键的文字表示（如 `"Shift+a"`、`"{Control+Left}"`）是如何被 `repr()` / `Parse()` 编码与解码的；
- 自己写一小段代码，把字符串 `"Shift+a"` 解析成 `KeyEvent`，并验证它的 `keycode` 与 `modifier`。

本讲是 u2 单元（核心运行时对象）的第一篇，后续的 Service/Session（u2-l2）会把 `KeyEvent` 一路送进引擎流水线。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：按键 = 「按了哪个键」+「当时的状态」。**
你在键盘上按下物理上的 `A` 键，这一事件同时携带两类信息：一是「按的是字母 A 这颗键」，二是「当时 Shift / Ctrl / Alt 有没有按下、是大写锁定（Caps Lock）开着的吗、是按下还是松开」。librime 用两个字段分别承载这两类信息：`keycode`（键值）和 `modifier`（修饰状态）。

**直觉二：修饰状态用「位掩码」表达。**
把 `modifier` 想象成一排开关，每一位代表一个修饰键：

\[ \text{modifier} = \cdots b_4\, b_3\, b_2\, b_1\, b_0 \]

其中 \(b_0=1\) 表示 Shift，\(b_2=1\) 表示 Control。于是「Shift+Control 同时按下」就是两位置 1，用按位或拼接：`kShiftMask | kControlMask`。检测某个修饰键是否按下，就用按位与：`(modifier & kShiftMask) != 0`。

**直觉三：按键需要可序列化成文字。**
输入方案（`.schema.yaml`）里经常要写「按下某键触发某行为」，比如「`Shift+Tab` 切换候选」。这要求按键能被写成一串文字存进配置文件，运行时再解析回来。`KeyEvent` 因此同时提供了二进制表示（两个 int）和文字表示（`repr()` / `Parse()`）。

> 术语提示：
> - **keysym（键值）**：librime 复用了 X11 的键值定义（`X11/keysym.h`），如 `XK_a = 0x61`、`XK_Return = 0xff0d`。`keycode` 字段存的就是 keysym。
> - **modifier（修饰位）**：Shift/Control/Alt/CapsLock/Super 等「状态键」的位掩码。
> - **可打印字符**：有对应 ASCII / Unicode 文字的键，如 `a`、`,`；**不可打印键**没有自然文字，如 `Return`、`F4`、方向键，只能用键名表示。

## 3. 本讲源码地图

本讲只围绕「按键表示」这一最小主题，涉及三个源码文件和两个测试文件：

| 文件 | 作用 |
| --- | --- |
| [src/rime/key_event.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.h) | `KeyEvent`（单键）与 `KeySequence`（按键序列）的类声明，定义双字段模型与文字编解码接口。 |
| [src/rime/key_event.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc) | `repr()` / `Parse()` 的具体实现：字符串与 `KeyEvent` 的互转。 |
| [src/rime/key_table.h](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_table.h) | modifier 位掩码枚举 `RimeModifier` 与四个「名字 ↔ 数值」查找函数声明。 |
| src/rime/key_table.cc | 修饰名表、键名表与四个查找函数的实现（本讲引用其中的表与函数）。 |
| test/key_event_test.cc | `KeyEvent` / `KeySequence` 的单元测试，是理解行为最好的依据。 |
| test/key_table_test.cc | 四个查找函数的单元测试。 |

一句话定位：`key_table.h` 提供「按键的字典」（位掩码定义 + 名字查表），`key_event.h` / `key_event.cc` 提供「按键的对象」（把字典里的数值封装成可用的 C++ 对象并可序列化）。

## 4. 核心概念与源码讲解

### 4.1 KeyEvent：按键的双字段模型

#### 4.1.1 概念说明

`KeyEvent` 是 librime 对「一次按键」的唯一内部表示。它的全部状态就是两个整数：`keycode_`（按了哪个键）和 `modifier_`（当时的修饰状态）。这种「键值 + 掩码」的二元组是输入法领域的通行做法——它既能精确描述物理动作（包括松开 `kReleaseMask`、大小写锁定 `kLockMask`），又便于用位运算组合多个修饰键。

为什么不用一个枚举把所有组合（`A`、`Shift+A`、`Ctrl+A`……）穷举出来？因为组合数是笛卡尔积，爆炸式增长；而拆成「基键 + 掩码」后，基键集合与修饰集合各自独立，组合靠按位或动态产生，存储和比较都极轻量。

#### 4.1.2 核心流程

一个 `KeyEvent` 在系统里的典型旅程：

1. 前端（Squirrel/Weasel 等）捕获 OS 层按键，得到一个 keysym 和一组修饰标志；
2. 前端调用 C API `process_key(session_id, keycode, mask)`；
3. librime 把这两个 int 包装成 `KeyEvent`，交给会话；
4. 会话把 `KeyEvent` 依次喂给引擎流水线上的 Processor（见 u6）。

包装这一步就发生在 `rime_api_impl.h`：

```c
KeyEvent(keycode, mask)
```

`KeyEvent` 一旦构造完成，就在引擎内部以 const 引用的形式传递，不再改动；它既是 Processor 的输入，也是配置文件里「按键绑定」的解析目标。

#### 4.1.3 源码精读

类声明非常简洁，先看字段与构造：

[key_event.h:17-22](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.h#L17-L22) 声明了 `KeyEvent`，并提供两个构造入口：一是 `(int keycode, int modifier)` 直接给两个 int，二是 `KeyEvent(const string& repr)` 从文字表示解析（解析失败则清零）。两个私有字段就是全部状态：

[key_event.h:54-55](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.h#L54-L55) —— `keycode_` 与 `modifier_`，默认都初始化为 0。

修饰位的「按位与」检测全部封装成布尔谓词：

[key_event.h:29-34](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.h#L29-L34) —— `shift()`/`ctrl()`/`alt()`/`caps()`/`super()`/`release()` 各自 `(modifier_ & 对应掩码) != 0`。注意 `caps()` 用的是 `kLockMask`（Caps Lock），`release()` 用的是 `kReleaseMask`（键松开事件）。

比较运算符只比较这两个字段：

[key_event.h:43-51](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.h#L43-L51) —— `operator==` 要求 `keycode` 与 `modifier` 都相等；`operator<` 先比 `keycode` 再比 `modifier`，使得 `KeyEvent` 可放入有序容器（如 `std::set`、`std::map` 的键），这正是「按键绑定表」所需要的。

最后，C API 把 `(keycode, mask)` 包装成 `KeyEvent` 的那一行，承接了上一篇 `process_key` 的讲解：

[rime_api_impl.h:171-178](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime_api_impl.h#L171-L178) —— 取到会话后 `session->ProcessKey(KeyEvent(keycode, mask))`。这就是「C 层两个 int → 引擎内 `KeyEvent`」的边界。

#### 4.1.4 代码实践（源码阅读型）

**目标**：建立「`keycode` 是物理键、`modifier` 是状态」的直觉，并验证「大写字母不蕴含 Shift」这一关键设计。

**步骤**：

1. 打开 [test/key_event_test.cc:79-93](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/key_event_test.cc#L79-L93)，细读 `PlainString` 测试及其上方注释。
2. 关注断言：对输入串 `"zyx123CBA"`，`ks[8]`（即 `'A'`）的 `keycode()` 等于 `XK_A`，但 `shift()` 为 **false**。
3. 阅读注释里给出的真实序列 `{Shift_L}{Shift+A}{Shift+Release+A}{Shift_L+Release}`，体会「Shift 是独立事件」。

**预期现象 / 结论**：

- `'A'` 解析后 `keycode = XK_A`（0x41），`modifier = 0`——大写字母键名本身不带 Shift 位；
- 想表达「Shift 修饰下的 A 键」，必须显式写 `Shift+A`（或对小写键名 `Shift+a`）。

> 说明：本步是阅读型实践，无需编译；若要运行，见 4.3.4 的构建说明。

#### 4.1.5 小练习与答案

**练习 1**：`KeyEvent(XK_A, kShiftMask)` 与 `KeyEvent(XK_a, 0)` 相等吗？为什么？

**答案**：不相等。`XK_A`（0x41）与 `XK_a`（0x61）是两个不同的 keysym，且前者的 `modifier` 含 `kShiftMask` 而后者为 0。`operator==` 要求两字段都相等（见 [key_event.h:43-45](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.h#L43-L45)），故不等。这正说明「键值」与「修饰状态」是彼此独立的维度。

**练习 2**：为什么 `KeyEvent` 要提供 `operator<`？

**答案**：为了让 `KeyEvent` 能作为 `std::set` / `std::map` 的键，从而实现「按键 → 行为」的有序绑定表。`operator<` 按 `(keycode, modifier)` 字典序比较（[key_event.h:47-51](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.h#L47-L51)）。

---

### 4.2 KeyTable：modifier 掩码与键名映射表

#### 4.2.1 概念说明

`key_event.h` 用到了一堆 `kShiftMask` / `kControlMask` 之类的常量，它们定义在 `key_table.h`。这个文件做两件事：

1. 用一个枚举 `RimeModifier` 给每个修饰键分配**一个二进制位**；
2. 声明四个函数，在「文字名字」与「数值」之间双向查表。

「位掩码」是这里的核心数据结构：每个修饰键独占一位，组合就是按位或，拆解就是逐位与。这样做的好处是 $n$ 个修饰键只需要一个 $n$ 位的整数就能表示任意组合，空间最省、运算最快。

#### 4.2.2 核心流程

`modifier_` 的位运算模型：

\[ \text{kShiftMask} = 2^0,\quad \text{kControlMask} = 2^2,\quad \text{kAltMask} = 2^3 \]

「Control + Alt」组合：

\[ \text{modifier} = \text{kControlMask}\ |\ \text{kAltMask} = 2^2 + 2^3 = 12 \]

检测 Control 是否按下：

\[ (\text{modifier}\ \&\ \text{kControlMask}) \ne 0 \]

键名查表则是一张静态数组：

- `modifier_name[]`：下标 = 位数（0=Shift, 2=Control, 30=Release…），值 = 名字字符串；
- `key_names` + `keys_by_name` / `keys_by_keyval`：键名串与 keysym 的双向映射。

四个查找函数就是这套表的访问入口：`RimeGetModifierByName`（名字→位掩码）、`RimeGetModifierName`（位掩码→名字，取最低有效位）、`RimeGetKeycodeByName`（键名→keysym）、`RimeGetKeyName`（keysym→键名）。

#### 4.2.3 源码精读

modifier 位掩码枚举（本讲最需要记住的一段）：

[key_table.h:14-46](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_table.h#L14-L46) —— 各位含义：

| 掩码 | 值 | 含义 |
| --- | --- | --- |
| `kShiftMask` | `1 << 0` | Shift |
| `kLockMask` | `1 << 1` | Caps Lock（`caps()` 用它） |
| `kControlMask` | `1 << 2` | Ctrl |
| `kAltMask` | `1 << 3` | Alt（等同 `kMod1Mask`） |
| `kSuperMask` | `1 << 26` | Super/Win/Cmd |
| `kReleaseMask` | `1 << 30` | 键松开（`release()` 用它） |
| `kModifierMask` | `0x5f001fff` | **所有有效修饰位的并集**，用于屏蔽越界位 |

注意 `kAltMask = kMod1Mask`（[key_table.h:19](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_table.h#L19)）——Alt 就是 X11 的 Mod1。`kModifierMask` 把零散分布在 0–12 位与 24–30 位的有效修饰位「圈」在一起，`repr()` 里会先用 `modifier_ & kModifierMask` 把无效位清掉。

修饰名表：

[key_table.cc:7-30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_table.cc#L7-L30) —— 数组下标即位数：`modifier_name[0]="Shift"`、`modifier_name[2]="Control"`、`modifier_name[26]="Super"`、`modifier_name[30]="Release"`；未使用的位填 `NULL`。

四个查找函数的声明：

[key_table.h:51-64](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_table.h#L51-L64) —— 注意两个约定：找不到修饰名时 `RimeGetModifierByName` 返回 `0`；找不到键名时 `RimeGetKeycodeByName` 返回 `XK_VoidSymbol`（一个表示「无效」的哨兵 keysym）。这两个返回值是 `Parse()` 判错的依据。

两个「按名查值」函数的实现：

[key_table.cc:2003-2013](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_table.cc#L2003-L2013) —— `RimeGetModifierByName` 线性扫描 `modifier_name[]`，命中则返回 `1 << i`。

[key_table.cc:2026-2033](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_table.cc#L2026-L2033) —— `RimeGetKeycodeByName` 扫描 `keys_by_keyval[]` 表比对键名串，命中返回 keysym，否则返回 `XK_VoidSymbol`。

「按值查名」函数的一个关键细节：`RimeGetModifierName` 只返回**最低有效位**的名字：

[key_table.cc:2015-2024](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_table.cc#L2015-L2024) —— 从低位向高位扫，遇到第一个为 1 的位就返回其名字。因此对组合掩码 `12`（Control|Alt）它只会返回 `"Control"`。要枚举所有修饰位，调用方需自己逐位移位（`KeyEvent::repr()` 正是这么做的，见 4.3.3）。

#### 4.2.4 代码实践（运行测试）

**目标**：用现成测试验证四个查找函数的约定。

**步骤**：

1. 阅读 [test/key_table_test.cc:10-30](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/key_table_test.cc#L10-L30)。
2. 在已按 u1-l2 构建（`BUILD_TEST=ON`）的 build 目录下运行测试二进制（所有用例合并为单个 `rime_test`，见 [test/CMakeLists.txt:3-8](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/CMakeLists.txt#L3-L8)），可加过滤只跑本主题用例：
   ```sh
   ./test/rime_test --gtest_filter='RimeKeyTableTest.*'
   ```

**需要观察的现象**：

- `RimeGetModifierByName("Control")` 等于 `kControlMask`，而 `"control"`（小写）返回 `0`——**名字大小写敏感**；
- `RimeGetModifierName(kControlMask | kReleaseMask)` 返回 `"Control"`（最低位），印证 4.2.3 的细节；
- `RimeGetKeycodeByName("abracadabra")` 与 `RimeGetKeycodeByName("Control+c")` 都返回 `XK_VoidSymbol`——后者说明「整串必须是一个键名，不能带修饰」。

**预期结果**：两个 `RimeKeyTableTest` 用例全部 `PASSED`。实际运行输出「待本地验证」（取决于是否已构建测试目标）。

#### 4.2.5 小练习与答案

**练习 1**：计算 `kShiftMask | kControlMask | kAltMask` 的十进制值。

**答案**：\(2^0 + 2^2 + 2^3 = 1 + 4 + 8 = 13\)。

**练习 2**：`RimeGetModifierName(13)` 会返回什么？为什么不是 `"Shift+Control+Alt"`？

**答案**：返回 `"Shift"`（最低有效位，\(2^0\)）。因为该函数只报告最低位（[key_table.cc:2015-2024](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_table.cc#L2015-L2024)）。要把组合掩码展开成完整名字串，由 `KeyEvent::repr()` 逐位循环完成，而不是单个函数调用。

---

### 4.3 文字编解码：repr / Parse 与 KeySequence

#### 4.3.1 概念说明

光有两个 int 还不够——配置文件里要写按键，必须有文字表示。`KeyEvent` 用一套小语法把按键序列化成字符串：

- **单字符**：可打印字符直接写字符本身，如 `"a"`、`","`；
- **键名**：不可打印键用键名，如 `"Return"`、`"space"`、`"F4"`；
- **组合键**：修饰名用 `+` 拼在前面，如 `"Control+a"`、`"Alt+F4"`；
- **无键名时**：用 4 位或 6 位十六进制，如 `"0xfffe"`。

`KeySequence` 是多个 `KeyEvent` 的序列（继承自 `vector<KeyEvent>`），用来表示「一连串按键」。为了避免键名与字面字符混淆，它引入花括号转义：单个普通字符直接写，特殊/组合键用 `{...}` 包起来，如 `"abc{Return}"`、`"{Control+Alt+Return}"`。

#### 4.3.2 核心流程

`KeyEvent::Parse(repr)` 的解析伪代码：

```
若 repr 为空                       → 失败
若 repr 长度为 1                   → keycode = 该字符的字节值（如 "a"→0x61, "+"→0x2b）
否则按 '+' 切分：
    对每个前缀 token：用 RimeGetModifierByName 查掩码，累加到 modifier_
                                  （查不到则失败）
    最后一个 token：用 RimeGetKeycodeByName 查 keysym
                                  （返回 XK_VoidSymbol 则失败）
```

`KeyEvent::repr()` 是反操作：逐位枚举 `modifier_` 的每个有效位，用 `RimeGetModifierName` 取名字并以 `+` 拼接，再追加键名（无键名时用十六进制）。

`KeySequence::Parse` 在外层扫描：遇到 `{` 就找配对的 `}`，把括号内整段交给 `KeyEvent::Parse`；否则每个字符自成一次 `KeyEvent`。`KeySequence::repr` 则对每个元素决定「直接输出字符」还是「用 `{...}` 包裹」。

#### 4.3.3 源码精读

从文字构造 `KeyEvent`，解析失败则清零：

[key_event.cc:14-17](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L14-L17)。

`repr()` 的实现，分两段——先拼修饰名、再拼键名：

[key_event.cc:19-49](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L19-L49)。要点有三：

1. 先用 `modifier_ & kModifierMask` 屏蔽掉无效位（[L23](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L23)）；
2. 逐位循环：`for (int i = 0; k; ++i, k >>= 1)`，对每个为 1 的位调用 `RimeGetModifierName(k << i)`（[L25-L32](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L25-L32)）。`k << i` 把当前位移回原始位置，配合「取最低位」语义精确得到该位名字；
3. 键名优先查 `RimeGetKeyName`，查不到再退化成十六进制（4 位或 6 位），keycode 完全非法时返回 `"(unknown)"`（[L35-L48](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L35-L48)）。

`Parse()` 的实现：

[key_event.cc:51-82](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L51-L82)。两处关键：

- **单字符快路径**（[L56-L57](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L56-L57)）：`repr.size() == 1` 时直接取 `repr[0]` 的字节值作 keycode。这解释了 [test/key_event_test.cc:64-70](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/key_event_test.cc#L64-L70) 的相等性测试——`KeyEvent("+")` 与 `KeyEvent("plus")` 与 `KeyEvent(XK_plus, 0)` 三者相等，因为 `'+'`(0x2b) 恰好就是 `XK_plus`；
- **多字符路径**（[L58-L80](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L58-L80)）：按 `+` 切分，前面每段必须是合法修饰名，最后一段必须是合法键名；任何一段查不到都记日志并返回 false。

`KeySequence` 的转义判定：

[key_event.cc:89-93](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L89-L93) —— `is_unescaped_character`：当且仅当「无修饰位 且 是 ASCII 可打印字符（0x20–0x7e） 且 不是花括号」时，该键在序列化时可直接写字符，否则需 `{...}` 包裹。

`KeySequence::repr` 与 `KeySequence::Parse`：

[key_event.cc:95-109](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L95-L109) 与 [key_event.cc:111-138](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L111-L138)。`Parse` 中 `{` 后若找不到配对 `}` 会报 `unparalleled brace` 错误（[L121-L124](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L121-L124)）。要表达字面意义的 `{`、`}`，需用键名 `{braceleft}`、`{braceright}`——见 [test/key_event_test.cc:119-125](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/key_event_test.cc#L119-L125) 的 `Stringification` 测试。

#### 4.3.4 代码实践（本讲核心任务）

**目标**：把字符串 `"Shift+a"` 解析成 `KeyEvent`，打印它的 `keycode` 与 `modifier`，验证组合键的掩码位，并与 `"A"` 对比。

**示例代码**（这是为学习编写的示例程序，非项目原有代码）：

```cpp
// demo_key_event.cc —— 示例代码
#include <iomanip>
#include <iostream>
#include <rime/key_event.h>
#include <rime/key_table.h>  // kShiftMask 等

int main() {
  rime::KeyEvent ke("Shift+a");

  std::cout << "repr      = " << ke.repr() << "\n";
  std::cout << "keycode   = " << std::dec << ke.keycode()
            << " (0x" << std::hex << ke.keycode() << ")\n";
  std::cout << "modifier  = " << std::dec << ke.modifier()
            << " (0x" << std::hex << ke.modifier() << ")\n";
  std::cout << "shift?    = " << std::boolalpha << ke.shift() << "\n";
  std::cout << "ctrl?     = " << ke.ctrl() << "\n";

  // 对比：单字符 "A" 不带 Shift 位
  rime::KeyEvent upper("A");
  std::cout << "'A'.keycode = 0x" << std::hex << upper.keycode()
            << ", shift? " << std::boolalpha << upper.shift() << "\n";

  return 0;
}
```

**操作步骤**：

1. 编译：因为 `key_event.h` 经由 `<rime/common.h>` 间接依赖 Boost 等，最稳妥的方式是把这段代码接进 librime 的构建环境。两种任选其一：
   - **方式 A（推荐，零侵入）**：把它改写成一个 GTest 用例，临时追加到 `test/key_event_test.cc` 末尾，然后用 u1-l2 的构建跑 `./test/rime_test --gtest_filter='你的用例名'`；学完删掉即可。
   - **方式 B（独立程序）**：在 build 目录里用 CMake 添加一个可执行目标，链接 `${rime_library}`（参考 `test/CMakeLists.txt` 的写法），再编译运行。
2. 运行并观察输出。

**需要观察的现象**：

- `"Shift+a"` 的 `keycode` 是 `XK_a`（0x61，**小写 a 的 keysym**），不是大写 `XK_A`（0x41）；
- `modifier` 是 `kShiftMask` = 1（0x1），`shift()` 为 true、`ctrl()` 为 false；
- `repr()` 原样回到 `"Shift+a"`，验证编解码可往返（round-trip）；
- 单字符 `"A"` 的 keycode 是 0x41、`shift()` 为 false——与 `"Shift+a"` 完全不同。

**预期结果**（由源码逻辑推得，实际运行输出「待本地验证」）：

```
repr      = Shift+a
keycode   = 97 (0x61)
modifier  = 1 (0x1)
shift?    = true
ctrl?     = false
'A'.keycode = 0x41, shift? false
```

**结论**：`keycode` 描述「按了哪颗键」，`modifier` 描述「当时的状态」，二者相互独立。这正是 4.1 里「大写字母不蕴含 Shift」的亲手验证。

#### 4.3.5 小练习与答案

**练习 1**：`KeyEvent("+")`、`KeyEvent("plus")`、`KeyEvent(XK_plus, 0)` 三者用 `operator==` 比较结果如何？

**答案**：三者两两相等。`"+"` 走单字符快路径，`'+'`(0x2b) 即 `XK_plus`；`"plus"` 走键名查表也得到 `XK_plus`；三者都是 `keycode=XK_plus, modifier=0`。这正是 [test/key_event_test.cc:64-70](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/key_event_test.cc#L64-L70) 的断言。

**练习 2**：为什么 `KeySequence::repr` 要把字面 `{` 输出成 `{braceleft}`？

**答案**：因为 `{` 和 `}` 在 `KeySequence::Parse` 中是**转义定界符**（[key_event.cc:118-126](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L118-L126)）。若直接输出 `{`，再次解析时会被当成转义序列开头。`is_unescaped_character` 因此显式排除了花括号（[key_event.cc:89-93](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L89-L93)），迫使其用键名形式表达，保证往返一致。

**练习 3**：`KeyEvent("0xfffe")` 合法吗？它的 `repr()` 是什么？

**答案**：合法。`"0xfffe"` 不是已知键名，但 `repr()` 对无键名的 keycode 会输出十六进制（[key_event.cc:39-48](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/src/rime/key_event.cc#L39-L48)）。注意：`Parse` 本身**只认修饰名与键名**，并不会把 `"0xfffe"` 解析成数值；但 [test/key_event_test.cc:20-25](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/key_event_test.cc#L20-L25) 表明，从数值 `0xfffe` 构造的 `KeyEvent`，其 `repr()` 会是 `"0xfffe"`。即十六进制是「输出格式」而非「输入语法」。

---

## 5. 综合实践

把本讲三个模块串起来，做一个 **KeySequence 往返（round-trip）实验**。

**任务**：构造一个包含普通字符、键名和组合键的按键序列，验证 `Parse → repr` 能无损还原，并解释每一步的内部表示。

**建议输入**：

```
ni hao{Shift+space}{Control+Alt+Return}
```

**步骤**：

1. 用 `KeySequence ks("ni hao{Shift+space}{Control+Alt+Return}")` 构造；
2. 遍历 `ks`，逐个打印每个 `KeyEvent` 的 `repr()`、`keycode()`（十六进制）和 `modifier()`（二进制或十六进制）；
3. 调用 `ks.repr()` 重新序列化，与原串对比；
4. 回答：`ks[3]`（第一个空格）与 `ks[7]`（`{Shift+space}`）的 `keycode` 是否相同？`modifier` 差在哪一位？

**预期分析**（由源码推得，实际「待本地验证」）：

- 两个空格的 `keycode` 都是 `XK_space`（0x20），但前一个 `modifier=0`，后一个 `modifier=kShiftMask`；
- `Control+Alt+Return` 的 `modifier = kControlMask | kAltMask = 4 | 8 = 12`，`keycode = XK_Return`；
- `ks.repr()` 应回到与输入等价的串（空格作为可打印字符可不带花括号直接输出，`Shift+space` 与 `Control+Alt+Return` 因含修饰位必须用 `{...}`）。

这个实验一次性印证了：双字段模型（4.1）、位掩码组合（4.2）、`{...}` 转义与往返一致（4.3）。

> 参考依据：[test/key_event_test.cc:103-117](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/key_event_test.cc#L103-L117) 的 `KeySequenceWithModifiedKeys` 用例几乎就是这个实验的「标准答案」，可以直接对照。

## 6. 本讲小结

- `KeyEvent` 是 librime 对「一次按键」的唯一内部表示，核心是两个 `int`：`keycode`（键值/keysym）+ `modifier`（修饰状态）。
- modifier 采用**位掩码**：`kShiftMask`、`kControlMask`、`kAltMask`、`kLockMask`（Caps）、`kSuperMask`、`kReleaseMask` 各占一位，组合用按位或，检测用按位与；`kModifierMask` 圈定所有有效位。
- `key_table.h` 提供「名字 ↔ 数值」四函数：修饰名与键名都可双向查表，查不到分别返回 `0` 与 `XK_VoidSymbol`，这是 `Parse` 判错的依据。
- 文字编解码由 `KeyEvent::repr()` / `Parse()` 完成：单字符走字节值快路径，多字符按 `+` 切分（前缀是修饰名、末段是键名）。
- `KeySequence` 是按键序列，用 `{...}` 转义不可打印/组合键；`is_unescaped_character` 决定字符是否可直接输出，保证 `Parse ↔ repr` 往返一致。
- C API 的 `process_key(keycode, mask)` 在边界处用 `KeyEvent(keycode, mask)` 把两个 int 封装成对象，交给会话与引擎流水线。

## 7. 下一步学习建议

本讲建立了「按键对象」的模型。下一篇 **u2-l2 Service 与 Session：会话管理** 将追踪 `process_key` 包装出的 `KeyEvent` 如何被会话接收、会话如何持有引擎、以及 Service 单例如何管理多个会话的生命周期。建议带着这个问题进入下一篇：一个 `KeyEvent` 从 `rime_api_impl.h` 的 `ProcessKey` 进入后，下一站是哪里？（提示：`Session::ProcessKey` → `Engine::ProcessKey`，后者正是 u6 按键流水线的起点。）

如果想立刻巩固本讲，推荐继续阅读：
- [test/key_event_test.cc](https://github.com/rime/librime/blob/d4c324ca988ed67f45e41524c2ab01d40cb55695/test/key_event_test.cc) 全文（不到 140 行，覆盖所有边界情形）；
- `src/rime/gear/speller.cc` 里 `Speller::ProcessKeyEvent` 如何读取 `key_event.keycode()` 决定是否把字母追加进输入串——这是 `KeyEvent` 被流水线消费的第一个真实例子（u6-l2 会详细讲）。

# 文件读写与位真数据交换

## 1. 本讲目标

位真协同仿真的最后一步，是把数据在三种语言之间搬来搬去：Python/MATLAB 生成激励、VHDL testbench 读取并运行、再把结果写回交由高级语言比对。这要求一种「写在文件里、跨语言都能还原成同一种定点数」的交换格式。`en_cl_fix` 提供了一组文件读写函数来完成这件事。

本讲聚焦这一组文件 IO 函数，学完后你应当能够：

- 说清 VHDL 八个读写函数 `cl_fix_read_int/real/bin/hex` 与 `cl_fix_write_int/real/bin/hex` 如何基于 `std.textio` 逐行读写文本文件。
- 理解**写函数的核心约定**：先把数据 `cl_fix_resize` 到参数 `f_fmt`，再序列化写出——因此「文件里的整数 1 对应 `f_fmt` 的一个 LSB」。
- 理解**读函数** `readline` → `read` → `cl_fix_from_*` 的三步流程，以及 `cl_fix_read_int` 用一个 `FracBits=0` 的 `TempFmt` 把整数「按位」装回定点数的技巧。
- 掌握 Python 独有的 `cl_fix_write_formats`：它写的不是数据，而是**格式定义文件**，用于让对端知道数据文件的每个字段是什么 `[S,I,F]`。
- 牢记一个跨语言事实：**这八个读写函数全部标为 deprecated**，官方推荐改用独立的 `en_cl_bittrue_pkg`；而在本库内部，`cl_fix_from_bits_as_int` / `cl_fix_get_bits_as_int`（u3-l1 已讲）才是更现代、更高效的位真数据桥梁。

## 2. 前置知识

本讲假设你已掌握：

- **u1-l2** 的 `[S, I, F]` 三元组（总位宽 \(W = S + I + F\)，数值 \(V = N \cdot 2^{-F}\)）。
- **u1-l5** 的四种饱和模式 `FixSaturate`（`None_s` / `Warn_s` / `Sat_s` / `SatWarn_s`）。
- **u3-l1** 的转换函数，尤其是 `cl_fix_from_real` / `cl_fix_from_int` / `cl_fix_from_bin` / `cl_fix_from_hex`，以及 `cl_fix_from_bits_as_int` / `cl_fix_get_bits_as_int` 这一对「按位、忽略小数点」的桥梁。
- **u3-l2 / u3-l3** 的 `cl_fix_resize`（舍入 + 饱和）——本讲所有写函数内部都会调用它。

这里再补一个本讲反复用到的直觉：

- **位串域 vs 实数域（再次强调）**：VHDL 把定点数存成 `std_logic_vector` 位串，要落盘必须先决定「这一串 0/1 在文件里以什么模样呈现」——整数、实数、二进制串还是十六进制串。这就是写函数的职责。Python/MATLAB 存的是 `double`，落盘天然就是实数。
- **「1 = f_fmt 的一个 LSB」**：文件里写的整数，并不是「数学意义上的整数」，而是「把定点位串当成整数读出来」的计数值。理解这一点，才能在读写两端对得上。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [vhdl/src/en_cl_fix_pkg.vhd](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd) | VHDL 包，**唯一**包含全部八个读写函数的实现。函数声明在 L488–L611，函数体在 L1854–L2023；包顶 `use std.textio.all` 在 L15。 |
| [python/src/en_cl_fix_pkg/en_cl_fix_pkg.py](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py) | Python 实现。**没有** `read/write_int/real/bin/hex`，只有 `cl_fix_write_formats`(L445)；位真桥梁改用 `cl_fix_from_bits_as_int`(L173) / `cl_fix_get_bits_as_int`(L184)。 |
| [matlab/src/cl_fix_write_int.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_write_int.m) | MATLAB 端仅有的两个写函数之一：把定点数写成「LSB 计数」整数。**注意它没有 `f_fmt` 参数**，恒以 `a_fmt` 的分辨率写出。 |
| [matlab/src/cl_fix_write_real.m](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_write_real.m) | MATLAB 端写实数函数，按格式推算小数位数后直接 `fprintf` 写 `double`。 |

> **跨语言函数可用性总览**（务必记住）：

| 函数 | VHDL | Python | MATLAB |
|------|:----:|:------:|:------:|
| `cl_fix_read_int / real / bin / hex` | ✓（已弃用） | ✗ | ✗ |
| `cl_fix_write_int` | ✓（已弃用） | ✗ | ✓ |
| `cl_fix_write_real` | ✓（已弃用） | ✗ | ✓ |
| `cl_fix_write_bin / hex` | ✓（已弃用） | ✗ | ✗ |
| `cl_fix_write_formats` | ✗ | ✓ | ✗ |
| `cl_fix_from_bits_as_int / get_bits_as_int` | ✓ | ✓ | ✗ |

也就是说：**完整的「四读四写」只在 VHDL 中存在，且全部 deprecated**；MATLAB 只有 `write_int` / `write_real`；Python 干脆不提供这些文本读写函数，而是用 `cl_fix_write_formats` 写格式定义、用 `bits_as_int` 走更高效的位真通道。这个不对称直接决定了你该如何设计跨语言数据流。

## 4. 核心概念与源码讲解

### 4.1 std.textio：VHDL 文件 IO 的底层机制

#### 4.1.1 概念说明

VHDL 没有像 Python `open()` 那样随手可用的文件 API。文件读写由标准库 `std.textio` 提供，它把文件看作「一行一行的文本」：

- **`text`**：文件类型，相当于一个打开的文件句柄。
- **`line`**：指向一行的缓冲区指针，读写都先在它上面操作。
- **`readline(f, l)`**：从文件 `f` 读一行到缓冲 `l`。
- **`read(l, v, ok)`**：从缓冲 `l` 里解析出一个 `v`（整数、实数、字符串等），`ok` 表示是否成功。
- **`write(l, v)`**：把 `v` 追加到缓冲 `l`。
- **`writeline(f, l)`**：把缓冲 `l` 作为一行写入文件 `f`（并清空缓冲）。

理解了这个「行缓冲」模型，本讲八个函数的套路就一目了然：读 = `readline` + `read`，写 = `write` + `writeline`。

#### 4.1.2 核心流程

```
读一行:  file ──readline──> line 缓冲 ──read──> VHDL 变量(integer/real/string)
写一行:  VHDL 变量 ──write──> line 缓冲 ──writeline──> file
```

两个细节：

1. **文件如何声明打开**：在进程/子程序的声明区写 `file stim_f : text open read_mode is "stim.txt";`（VHDL-2008 也可用 `file_open`）。本库的读写函数把 `file` 作为参数传入，由调用者负责打开。
2. **为什么读函数是 `impure`**：函数若依赖外部状态（这里是「文件当前读到第几行」），VHDL 要求声明为 `impure function`。纯函数对相同输入必须返回相同值，而读文件每次调用都推进一行——所以 `cl_fix_read_*` 都是 `impure`。写函数是 `procedure`，不需要这个关键字。

#### 4.1.3 源码精读

包顶部引入 `std.textio`，这是所有文件函数的前提：

[vhdl/src/en_cl_fix_pkg.vhd:L15-L15](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L15-L15) — 引入 `std.textio.all`，提供 `text` / `line` / `readline` / `read` / `write` / `writeline`。

testbench 里有一个最小、最清晰的 `textio` 使用样例——自定义的 `print` 过程，它把字符串写到标准输出 `output`：

[vhdl/tb/en_cl_fix_pkg_tb.vhd:L83-L88](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L83-L88) — `write(l, text)` 把字符串塞进行缓冲，`writeline(output, l)` 把缓冲作为一行写出。`output` 是 `std.textio` 预定义的「标准输出文件」。把 `output` 换成一个真实打开的 `text` 文件，就是写文件；反之 `readline` + `read` 就是读文件。

#### 4.1.4 代码实践

**实践目标**：在阅读任何 `cl_fix_read/write_*` 实现之前，先吃透 `textio` 的「行缓冲」模型。

**操作步骤**：

1. 打开 [vhdl/tb/en_cl_fix_pkg_tb.vhd:L83-L88](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/tb/en_cl_fix_pkg_tb.vhd#L83-L88) 的 `print` 过程。
2. 在纸上画出 `write(l, text)` 与 `writeline(output, l)` 两步各自改变了什么（缓冲内容 / 输出流）。
3. 思考：如果把 `writeline(output, l)` 连续调用两次、中间不改 `l`，第二次会写出什么？（答：空行，因为 `writeline` 写完会清空缓冲。）

**需要观察的现象 / 预期结果**：理解「`write` 累加进缓冲、`writeline` 一次性吐出一行并清空」的成对关系。这是后续八个函数的最小积木。

> 待本地验证：若你在 Modelsim 中给 `print` 喂不同字符串并多次调用，可在 Transcript 窗口逐行看到输出，印证「一次 `writeline` = 一行」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cl_fix_read_int` 必须是 `impure function`，而 `cl_fix_write_int` 是 `procedure` 就不需要 `impure`？

**答案**：`read_int` 的返回值取决于「文件当前读到第几行」这一外部状态，相同参数多次调用会返回不同结果，故 VHDL 强制要求 `impure`；`procedure` 本就允许修改外部状态（包括推进文件指针），没有「纯函数」约束，因此无需该关键字。

**练习 2**：`std.textio` 里 `readline` 和 `read` 的分工是什么？

**答案**：`readline(f, l)` 负责「物理」层面从文件取一行到缓冲 `l`；`read(l, v, ok)` 负责「解析」层面从缓冲 `l` 里取出一个具体类型的值（`integer` / `real` / `string`）。两者分离，使得一行可以多次 `read` 出多个字段。

---

### 4.2 四个写函数 write_int/real/bin/hex：先 resize 到 f_fmt 再序列化

#### 4.2.1 概念说明

四个写函数的职责，是把一个 `a_fmt` 格式的定点数 `a` 写入文本文件，写成整数、实数、二进制串或十六进制串四种模样之一。它们共享一个统一约定：

> 写之前，先用 `cl_fix_resize(a, a_fmt, f_fmt, round, saturate)` 把数据从 `a_fmt` 转换到 `f_fmt`，然后把 `f_fmt` 下的位串序列化写出。

这里第二个格式参数 `f_fmt` 是关键。它表示「文件里这一行数据采用什么定点格式」。函数注释里那句**「1 corresponds to one LSB of the format f_fmt」**正是这个意思：写出的整数，是 `f_fmt` 位串当成整数读出来的计数值，1 就是 `f_fmt` 的最低有效位。

为什么要单独给一个 `f_fmt`，而不是直接用 `a_fmt`？因为这样可以把「内部计算的精度」和「落盘交换的精度」解耦——你可以在内部用高精度 `a_fmt` 运算，再统一收缩到一个约定好的 `f_fmt` 写出，方便对端读取。

#### 4.2.2 核心流程

四个写函数的统一三步：

```
输入: a (a_fmt), f_fmt, round(默认 Trunc_s), saturate(默认 Warn_s)
   │
   ├─ 1) f_v := cl_fix_resize(a, a_fmt, f_fmt, round, saturate)   # 统一先转换
   │
   ├─ 2) 把 f_v 序列化:
   │      · write_int  : temp_v := to_integer(signed/unsigned(f_v))   # 整数 = LSB 计数
   │      · write_real : temp_v := cl_fix_to_real(f_v, f_fmt)         # 实数
   │      · write_bin  : temp_v := cl_fix_to_bin(f_v, f_fmt)          # 二进制串
   │      · write_hex  : temp_v := cl_fix_to_hex(f_v, f_fmt)          # 十六进制串
   │
   └─ 3) write(line_v, temp_v); writeline(f, line_v)                # 写一行
```

注意默认值：`round` 默认 `Trunc_s`（零成本截断），`saturate` 默认 `Warn_s`（越界只告警、不夹紧）。所以**默认配置下，若 `a` 在 `f_fmt` 下越界，写出的是回绕后的位串，仅给一条 warning**。

> **跨语言差异（MATLAB）**：MATLAB 的 `cl_fix_write_int` **没有 `f_fmt` 参数**，它恒以 `a_fmt` 的分辨率写出（先 `cl_fix_from_real(a, a_fmt, Sat.Warn_s)` 再乘 `2^FracBits`）。所以 MATLAB↔VHDL 互换时，要让 VHDL 端的 `f_fmt` 等于 MATLAB 端的 `a_fmt`，才能位真对齐。

#### 4.2.3 源码精读

四个写函数的声明（注意都是 `procedure`，且都带 deprecated 警告）：

[vhdl/src/en_cl_fix_pkg.vhd:L538-L611](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L538-L611) — 四个写函数声明，统一形参 `(a, a_fmt, file f, f_fmt, round := Trunc_s, saturate := Warn_s)`；注释里的 `\note 1 corresponds to one LSB of the format f_fmt` 与 `\warning ... deprecated, use en_cl_bittrue_pkg instead`。

`cl_fix_write_int` 的实现，是最能体现「resize → 序列化」套路的代表：

[vhdl/src/en_cl_fix_pkg.vhd:L1951-L1969](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1951-L1969) — 第 1961 行 `f_v := cl_fix_resize(a, a_fmt, f_fmt, round, saturate)` 把数据转到 `f_fmt`；第 1962–1966 行按 `f_fmt.Signed` 选用 `signed` / `unsigned` 解释位串并 `to_integer`，得到「LSB 计数」整数；最后 `write` + `writeline` 落盘。

另外三个写函数结构与 `write_int` 完全对称，只是序列化那一步换成了 `cl_fix_to_real` / `cl_fix_to_bin` / `cl_fix_to_hex`：

[vhdl/src/en_cl_fix_pkg.vhd:L1973-L2023](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1973-L2023) — `write_real`(L1983–1984 先 resize 再 `to_real`)、`write_bin`(L2001–2002 先 resize 再 `to_bin`)、`write_hex`(L2019–2020 先 resize 再 `to_hex`)，套路一字不差。

MATLAB 端 `write_int` 没有 `f_fmt`，体现的是「落盘分辨率即 `a_fmt`」的简化约定：

[matlab/src/cl_fix_write_int.m:L20-L30](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/matlab/src/cl_fix_write_int.m#L20-L30) — 第 25 行 `cl_fix_from_real(a, a_fmt, Sat.Warn_s)` 量化装箱，第 26 行 `a*2^a_fmt.FracBits` 左移到整数，第 29 行 `fprintf('%.0f \n', a)` 写成不带指数的整数（避免 `1e4` 之类格式）。这一步左移 `2^FracBits`，正是「把定点小数变成 LSB 计数整数」，与 VHDL `write_int` 里 `to_integer` 的语义对应。

#### 4.2.4 代码实践

**实践目标**：在不跑仿真的前提下，手算验证「1 = f_fmt 的一个 LSB」这条约定。

**操作步骤**：

1. 取 `a_fmt = (true, 3, 2)`（宽 6 位），`a` 的实数值 = `1.25`。
2. 设 `f_fmt = (true, 3, 2)`（与 `a_fmt` 相同，方便对照）。手算 `a` 的 6 位补码位串：\(N = 1.25 \times 2^{2} = 5\)，即二进制 `000101`。
3. 套用 `write_int` 的逻辑：`f_v` 同样是 `000101`，`to_integer(signed("000101")) = 5`。所以文件里写出 `5`。
4. 用 Python 旁路验证这个「LSB 计数」（Python 没有 `write_int`，但有等效的 `cl_fix_get_bits_as_int`）：

   ```python
   # 示例代码：用 Python 等效复现 write_int 的 "LSB 计数"
   import sys; sys.path.append("../src")
   from en_cl_fix_pkg import *
   a = cl_fix_from_real(1.25, FixFormat(True, 3, 2), FixSaturate.SatWarn_s)
   print(cl_fix_get_bits_as_int(a, FixFormat(True, 3, 2)))   # 预期 5
   ```

**需要观察的现象 / 预期结果**：Python 应打印 `5`，与第 3 步手算一致，印证「整数 5 = `f_fmt` 下 5 个 LSB = 实数 1.25」。

> 待本地验证：VHDL 端若在 testbench 里实际调用 `cl_fix_write_int` 写盘，应得到内容为 `5` 的一行文本。

#### 4.2.5 小练习与答案

**练习 1**：若把上面的 `f_fmt` 改成 `(true, 3, 0)`（无小数位），`a = 1.25` 会写出什么整数？为什么？

**答案**：`cl_fix_resize` 会把 `1.25` 从 `(true,3,2)` 截断（默认 `Trunc_s`）到 `(true,3,0)`，得到 `1`，位串 `000001`，`to_integer = 1`，写出 `1`。小数部分被默认截断丢弃，所以「1 = `f_fmt` 的 1 个 LSB」在这里就是「1 = 实数 1」。

**练习 2**：为什么四个写函数的 `saturate` 默认是 `Warn_s` 而不是 `SatWarn_s`？

**答案**：`Warn_s` 只告警、不夹紧（越界回绕），保留与「纯文本交换」一致的「写出原始位串」语义；若默认夹紧，则数据会在落盘时被悄悄修改，反而让对端难以发现源数据已越界。设计者选择「 loudly wrap」而非「silently clip」。

---

### 4.3 四个读函数 read_int/real/bin/hex：readline + read + from_*

#### 4.3.1 概念说明

四个读函数是写函数的镜像：从文本文件读一行，解析成整数 / 实数 / 二进制串 / 十六进制串，再用 u3-l1 讲过的 `cl_fix_from_int` / `cl_fix_from_real` / `cl_fix_from_bin` / `cl_fix_from_hex` 把它装进 `result_fmt` 格式的定点数。

读函数的统一形参是 `(file a, result_fmt, ...)`：`result_fmt` 决定返回的 `std_logic_vector` 的宽度与解释。注意几个不对称：

- `read_int` / `read_real` 带 `saturate` 参数（默认 `SatWarn_s`），因为它们要量化装箱。
- `read_bin` / `read_hex` **没有** `saturate` 参数——二进制/十六进制串本身就直接是位串，长度必须恰好等于 `cl_fix_width(result_fmt)`，不存在量化空间。

#### 4.3.2 核心流程

```
读一行:  readline(a, line_v)                 # 物理取一行
解析:    read(line_v, temp_v, ok_v)          # 解析为 integer/real/string
装填:    result := cl_fix_from_int/real/bin/hex(temp_v, <fmt>, ...)   # 装进 result_fmt
失败:    ok_v=false 时 assert severity error
```

`read_int` 里有一个值得专门讲的技巧。它并不直接 `cl_fix_from_int(temp_v, result_fmt)`，而是先构造一个中间格式：

\[
\text{TempFmt} = (\,\text{result\_fmt.Signed},\ \text{result\_fmt.IntBits} + \text{result\_fmt.FracBits},\ 0\,)
\]

注意 `TempFmt` 的总位宽 \(= S + (I + F) + 0 = S + I + F\)，**与 `result_fmt` 完全相等**。于是 `from_int(temp_v, TempFmt)` 产出的位串宽度恰好等于 `result_fmt` 的宽度，可以直接当作 `result_fmt` 的位串返回。这个 `FracBits=0` 的 `TempFmt` 的作用是：**把文件里的整数「原封不动」当成一串比特塞进一个等宽的位槽里**，让数学上的整数与定点位串在「位级」上一一对应——这正是「1 = 一个 LSB」约定的读端实现。

#### 4.3.3 源码精读

`cl_fix_read_int` 的实现，重点看 `TempFmt_c` 的构造与 `from_int` 的调用：

[vhdl/src/en_cl_fix_pkg.vhd:L1855-L1880](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1855-L1880) — 第 1859–1864 行构造 `TempFmt_c`（`FracBits=>0`，`IntBits=>I+F`，宽度与 `result_fmt` 相等）；第 1870–1871 行 `readline` + `read` 取出整数 `temp_v`；第 1873 行 `cl_fix_from_int(temp_v, TempFmt_c, saturate)` 把整数按等宽位槽装回；第 1875–1877 行在读取失败时 `assert severity error`（这正是 u2-l2 讲过的 `###ERROR###` 失败标记来源之一）。

其余三个读函数结构完全对称：

[vhdl/src/en_cl_fix_pkg.vhd:L1884-L1947](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L1884-L1947) — `read_real`(L1896 直接 `from_real(temp_v, result_fmt, saturate)`)、`read_bin`(L1918 `from_bin(temp_v, result_fmt)`)、`read_hex`(L1940 `from_hex(temp_v, result_fmt)`)。注意 `read_bin` / `read_hex` 的 `temp_v` 是 `string(cl_fix_width(result_fmt) downto 1)`——字符串长度必须恰为位宽，否则解析失败。

#### 4.3.4 代码实践

**实践目标**：手算 `write_int` → `read_int` 的往返一致性，验证 `TempFmt` 技巧。

**操作步骤**：

1. 沿用 4.2.4 的场景：`write_int` 在 `f_fmt=(true,3,2)` 下对 `1.25` 写出了文件内容 `5`。
2. 现在用 `read_int` 把 `5` 读回，设 `result_fmt=(true,3,2)`（与写出端 `f_fmt` 一致）。
3. 套源码逻辑：`TempFmt_c = (true, 3+2=5, 0)`，宽 6 位；`from_int(5, (true,5,0))` 把 5 装进 6 位有符号位串 = `000101`；返回的 `result_v` 宽 6 位 = `000101`，按 `(true,3,2)` 重新解释 = \(5 \times 2^{-2} = 1.25\)。
4. 用 Python 等效旁路验证 `from_int` 那一步（Python 没有 `from_int`，但可用 `from_bits_as_int` 模拟「等宽位槽装整数」）：

   ```python
   # 示例代码：用 from_bits_as_int 模拟 read_int 的 TempFmt 装填
   import sys; sys.path.append("../src")
   from en_cl_fix_pkg import *
   v = cl_fix_from_bits_as_int(5, FixFormat(True, 3, 2))   # 整数 5 直接当位串装进 (true,3,2)
   print(v)   # 预期 1.25
   ```

**需要观察的现象 / 预期结果**：Python 打印 `1.25`，与写出端的原始值一致，完成位真往返。`from_bits_as_int` 与 VHDL `read_int` 的 `TempFmt` 装填在「按位装整数」这一语义上完全等价。

> 待本地验证：VHDL 端若把 `5` 写入 stim 文件再用 `cl_fix_read_int` 读回，应得到与 `1.25` 等价的 `result_fmt` 位串。

#### 4.3.5 小练习与答案

**练习 1**：`read_int` 为什么不直接 `cl_fix_from_int(temp_v, result_fmt)`，而要先构造 `FracBits=0` 的 `TempFmt`？

**答案**：因为文件里的整数是「位串当成整数读出的计数」，必须按「等宽位槽」原封不动塞回，才能保证位真。若直接用 `result_fmt`（带 `FracBits`）调用 `from_int`，`from_int` 会把整数当成「数学整数」再量化到 `result_fmt`，可能改变位串。`TempFmt` 把 `FracBits` 全挪到 `IntBits`、宽度不变，等于声明「这一串比特就是整数本身」，从而保留原始位串。

**练习 2**：`read_bin` 为什么没有 `saturate` 参数？

**答案**：二进制串本身就是定宽位串，长度被强制要求等于 `cl_fix_width(result_fmt)`，不存在「值越界需要夹紧」的问题——它就是逐位填入。只有 `read_int` / `read_real` 这种「从一个更大域映射进定点网格」的路径才需要量化与饱和，故才有 `saturate`。

---

### 4.4 cl_fix_write_formats、已弃用趋势与迁移路径

#### 4.4.1 概念说明

前面三节解决了「数据怎么读写」，但跨语言交换还差一块拼图：**对端怎么知道这一列数据是什么 `[S,I,F]` 格式？** 如果 Python 写出一批数据、VHDL 要读，VHDL 必须事先知道每个字段的 `FixFormat_t`。`cl_fix_write_formats` 就是干这个的——它写出的不是数据，而是一份**格式清单文件**：第一行是字段名表头，其后每个字段一行 `cl_fix_string_from_format` 文本。

更重要的是本讲的整体结论：**本库的八个读写函数全部标为 deprecated**。每个函数声明的注释里都有同一句 `\warning This function is deprecated, use en_cl_bittrue_pkg instead`。`en_cl_bittrue_pkg` 是 PSI 另一个独立的位真数据交换包（不在本仓库内），提供了更鲁棒、更通用的跨语言数据流机制。本库内部，现代的位真桥梁是 u3-l1 讲过的 `cl_fix_from_bits_as_int` / `cl_fix_get_bits_as_int`——它「忽略小数点、按位搬运」，比文本式的 int/real/bin/hex 更高效，也无需逐字段约定字符串格式。

#### 4.4.2 核心流程

`cl_fix_write_formats` 的写出格式：

```
# name1,name2,name3              <- 表头：逗号分隔的字段名
(S1, I1, F1)                     <- 每行一个 cl_fix_string_from_format(fmt)
(S2, I2, F2)
(S3, I3, F3)
```

注意 Python 的 `cl_fix_string_from_format` 输出**大写带空格**（如 `(True, 3, 2)`，见 u1-l3），与 VHDL/MATLAB 的小写无空格不同——若对端用 VHDL 的 `cl_fix_format_from_string`（u7-l3 会讲）解析，需留意大小写处理。

#### 4.4.3 源码精读

Python 端唯一的文件 IO 函数，位于专门的 `# File I/O` 分节下：

[python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:L441-L457](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L441-L457) — `cl_fix_write_formats(fmts, names, filename)`：第 449 行写表头 `# name1,name2,...`；第 453–454 行允许 `fmts` 为标量（自动升维）；第 456–457 行循环对每个格式调用 `cl_fix_string_from_format(fmt)` 写一行。

deprecation 警告的权威来源——四个写函数声明里都带同一句：

[vhdl/src/en_cl_fix_pkg.vhd:L546-L546](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/vhdl/src/en_cl_fix_pkg.vhd#L546-L546) — `write_int` 的 `\warning This function is deprecated, use en_cl_bittrue_pkg instead`（`write_real` / `write_bin` / `write_hex` 以及四个 `read_*` 函数的同款警告分别在 L510 / L582 / L601 / L497 / L522 / L532）。

现代位真桥梁的 Python 端实现（u3-l1 已详述，此处只作定位）：

[python/src/en_cl_fix_pkg/en_cl_fix_pkg.py:L173-L188](https://github.com/paulscherrerinstitute/en_cl_fix/blob/7f7aa80f79caf9eefcbb9946feabc882b98bb4aa/python/src/en_cl_fix_pkg/en_cl_fix_pkg.py#L173-L188) — `cl_fix_from_bits_as_int` / `cl_fix_get_bits_as_int`：直接以「整数 = 位串」搬运，跳过文本序列化，是官方推荐的位真数据交换底座。

#### 4.4.4 代码实践

**实践目标**：真正运行 `cl_fix_write_formats`，观察它产出的格式定义文件长什么样。

**操作步骤**：

1. 进入 `python/unittest` 目录（保证 `sys.path.append("../src")` 能找到包，见 u2-l1）。
2. 新建一个脚本 `gen_formats.py`（示例代码）：

   ```python
   # 示例代码
   import sys; sys.path.append("../src")
   from en_cl_fix_pkg import *

   fmts = [FixFormat(True, 3, 2), FixFormat(False, 8, 0), FixFormat(True, 1, 8)]
   names = ["gain", "count", "dc"]
   cl_fix_write_formats(fmts, names, "formats.txt")
   print(open("formats.txt").read())
   ```

3. 运行 `python3 gen_formats.py`。

**需要观察的现象 / 预期结果**：终端应打印：

```
# gain,count,dc
(True, 3, 2)
(False, 8, 0)
(True, 1, 8)
```

印证「表头 + 每行一个格式串」的结构，以及 Python 大写带空格的输出风格。

> 待本地验证：实际运行上述脚本确认输出格式（本讲未在沙箱内执行）。

#### 4.4.5 小练习与答案

**练习 1**：`cl_fix_write_formats` 写出的是「数据」还是「元数据」？它和 `cl_fix_write_int` 的关系是什么？

**答案**：是**元数据**（格式清单），不含任何具体数值。它回答「数据文件里每个字段是什么 `[S,I,F]`」，而 `cl_fix_write_int` 回答「某个具体定点数的值是多少」。一个完整的跨语言交换链路通常两者都需要：先用 `write_formats` 声明字段格式，再用 `write_int` 等逐字段写数据。

**练习 2**：既然这八个读写函数都 deprecated，为什么本讲还要花大篇幅讲它们？

**答案**：其一，大量存量 testbench 与遗留代码仍在使用它们，读懂它们是维护旧代码的前提；其二，它们是理解 `en_cl_bittrue_pkg` 设计动机的最佳切入点——你会看到文本式 int/real/bin/hex 交换的局限（要逐字段约定格式、要文本序列化、跨语言签名不对称），从而理解为何官方要另起炉灶。其三，`read_int` 的 `TempFmt` 技巧本身就是理解「位串 vs 数值」的绝佳练习。

---

## 5. 综合实践

把本讲全部内容串起来，设计一个**最小的跨语言位真数据交换链路**（Python 造数据 → VHDL 读）：

1. **写格式定义文件**：在 `python/unittest` 下运行下面的示例脚本，生成 `formats.txt` 与一个数据文件 `data.txt`：

   ```python
   # 示例代码：Python 端生成格式清单 + 数据
   import sys; sys.path.append("../src")
   from en_cl_fix_pkg import *

   fmt = FixFormat(True, 3, 2)             # 一个 (true,3,2) 字段，宽 6 位
   cl_fix_write_formats([fmt], ["a"], "formats.txt")

   # 造 3 个测试值：1.25, -1.0, 0.75
   vals = [1.25, -1.0, 0.75]
   fxp = cl_fix_from_real(vals, fmt, FixSaturate.SatWarn_s)
   # 用 LSB 计数写出（等效于 VHDL write_int 在 f_fmt=fmt 下的输出）
   bits = cl_fix_get_bits_as_int(fxp, fmt)
   with open("data.txt", "w") as f:
       for b in bits:
           f.write(f"{b}\n")
   ```

2. **手算预期**：先在纸上算出三个值在 `(true,3,2)` 下的 6 位补码位串与对应整数（提示：\(N = V \cdot 2^{2}\)），再核对脚本写出的 `data.txt` 每行整数是否与之相符。
3. **设计 VHDL 读端约定**：回答——若用 VHDL `cl_fix_read_int` 逐行读取 `data.txt`，应当：
   - 把 `data.txt` 以 `text open read_mode` 打开；
   - 调用 `cl_fix_read_int(stim_f, result_fmt)`，其中 `result_fmt` **必须等于** Python 端的 `fmt = (true,3,2)`，且与写出端 `f_fmt` 一致，才能位真还原；
   - 在 testbench 进程里循环三次，每行读出一个 `std_logic_vector(5 downto 0)`，再用 `cl_fix_to_real` 转回实数，期望得到 `1.25 / -1.0 / 0.75`。
4. **解释迁移理由**：用一段话说明为什么官方推荐改用 `en_cl_bittrue_pkg`——至少覆盖三点：① 文本式 int/real/bin/hex 需要逐字段手工约定 `f_fmt`、易错；② 跨语言签名不对称（VHDL 全套、MATLAB 只有两个、Python 只有 `write_formats`）；③ 文本序列化开销大，而 `bits_as_int` / `en_cl_bittrue_pkg` 直接按位搬运二进制更高效且位真。

> 待本地验证：步骤 1–2 可在装有 numpy 的 Python 环境直接运行；步骤 3 的 VHDL 部分需在 Modelsim 中编译 `en_cl_fix_pkg.vhd` 后写一个最小 testbench 验证（参考 u2-l2 的 `sim.tcl` 流程）。

## 6. 本讲小结

- VHDL 八个读写函数 `cl_fix_read/write_int/real/bin/hex` 全部建立在 `std.textio` 的「行缓冲」模型上：读 = `readline` + `read`，写 = `write` + `writeline`；读函数因依赖文件位置而声明为 `impure`。
- **写函数的核心约定**是「先 `cl_fix_resize` 到 `f_fmt` 再序列化」，因此文件里的「整数 1 对应 `f_fmt` 的一个 LSB」；默认 `round=Trunc_s`、`saturate=Warn_s`（越界回绕并告警）。
- **读函数**三步走 `readline → read → cl_fix_from_*`；其中 `read_int` 用一个 `FracBits=0`、宽度与 `result_fmt` 相等的 `TempFmt`，把整数「按位」塞进等宽位槽，是「1 = 一个 LSB」的读端实现。
- **跨语言不对称**：四读四写只在 VHDL 齐全，MATLAB 仅 `write_int` / `write_real`（且 `write_int` 没有 `f_fmt`），Python 完全不提供这些文本读写函数。
- `cl_fix_write_formats`（仅 Python）写出的是**格式定义元文件**（表头 + 每行一个 `cl_fix_string_from_format`），用于让对端知道数据字段的 `[S,I,F]`。
- **这八个函数全部 deprecated**，官方推荐独立的 `en_cl_bittrue_pkg`；本库内部现代位真桥梁是 `cl_fix_from_bits_as_int` / `cl_fix_get_bits_as_int`（u3-l1），按位搬运、无需文本序列化。

## 7. 下一步学习建议

- 若想继续深挖「位真数据交换」的现代方案，建议直接阅读 PSI 的 `en_cl_bittrue_pkg`（独立仓库），对比它与本讲文本式方案的差异。
- 本库内部，可结合 **u3-l1** 复习 `cl_fix_from_bits_as_int` / `cl_fix_get_bits_as_int`，并在 **u6-l1 / u6-l2** 中看 `wide_fxp.to_uint64_array` / `FromUint64Array` 如何为大位宽数据提供打包交换——那是 >53 位场景下 `bits_as_int` 思路的工业级延续。
- 若对格式字符串的**反向解析**（把 `(true,3,2)` 文本变回 `FixFormat_t`）感兴趣，可预习 **u7-l3**，那里会讲 `cl_fix_format_from_string` 及其底层字符串解析工具链，正是本讲 `cl_fix_write_formats` 产出文件的对端解析器。

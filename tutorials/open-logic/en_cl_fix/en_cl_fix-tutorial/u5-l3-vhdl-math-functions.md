# VHDL 数学函数：add/sub/mult 与符号运算

## 1. 本讲目标

本讲是 U5「VHDL 包内部实现」的第三讲，承接 u5-l2（转换层 `convert` / `round` / `saturate` / `resize` 实现），把视线从「格式搬运」推进到「真正做运算」的数学函数层。

读完本讲，你应当能够：

- 看懂 `cl_fix_add` / `cl_fix_sub` / `cl_fix_mult` 等 VHDL 函数共享的「`convert` 到 `mid_fmt` → 运算 → `resize`」三段式模板。
- 解释为什么所有加/减运算都强制走 `numeric_std.signed`，而不是混用 `signed`/`unsigned`。
- 理解 `cl_fix_mult` 为何要在函数体内部临时定义 `signed * unsigned` 的乘法重载，以及 `resize_sensible` 在乘法截断中起的决定性作用。
- 说明 `cl_fix_shift` 如何「不做任何位移、只改格式标注」就完成了无损移位。
- 读懂 `cl_fix_compare` 的对齐比较与 `cl_fix_sign` 的符号提取逻辑。

本讲只讲 `hdl/en_cl_fix_pkg.vhd` 包体里的**数学函数实现**（u5-l1 讲过它们的包头声明，u5-l2 讲过它们依赖的转换层）。格式预测（`cl_fix_add_fmt` 等）的推导在 U3 已讲透，本讲只把它们当作「已经算好的 `mid_fmt`」来使用。

## 2. 前置知识

在进入源码前，先建立三条直觉。

### 2.1 补码让有符号和无符号的加/减完全一致

定点数在硬件里就是一串比特。同样的比特串，若最高位被约定为符号位（补码），它就是有符号数；否则就是无符号数。奇妙的是：**只要位数对齐，补码的加法/减法与无符号的加法/减法在比特层面完全相同**。

例如 4 位运算 `1011 + 0001`：

- 当作无符号：\(11 + 1 = 12\)，结果是 `1100`。
- 当作有符号补码：\( (-5) + 1 = -4\)，结果也是 `1100`（`1100` 作为补码是 \(-4\)）。

位串 `1100` 一模一样，只是解释不同。这就是为什么本讲的加/减函数「先 `convert` 对齐、再统一用 `signed` 做运算」是合法的——运算结果位串对两种解释都正确。

### 2.2 「全精度中间格式 mid_fmt」是三段式的心脏

硬件里每根信号的位宽在**综合期**就被钉死。一个加法器，输入可能来自不同格式 `a_fmt`、`b_fmt`，输出又可能要求缩窄到 `result_fmt`。`en_cl_fix` 的统一做法是：

1. 用纯函数 `cl_fix_add_fmt(a_fmt, b_fmt)` 在**综合期**算出一个**最坏情况下也装得下**的中间格式 `mid_fmt`（U3 已推导，本讲直接用）。
2. 把两个输入 `convert`（对齐二进制小数点、补齐位宽）到这个 `mid_fmt`。
3. 在 `mid_fmt` 下做**无损**运算（加/减/乘本身不丢精度）。
4. 最后用 `cl_fix_resize` 把结果收敛到调用者想要的 `result_fmt`（这一步才发生舍入与饱和）。

也就是说：**精度损失只发生在最后的 `resize`，运算本身永远无损**。这与 Python 参考模型（U4）的设计完全一致。

### 2.3 NullFixFormat_c 是「不指定结果格式」的哨兵

数学函数都有一个 `result_fmt : FixFormat_t := NullFixFormat_c` 的默认参数。`NullFixFormat_c = (0,0,-1)` 是个位宽为负的非法格式，它在这里表示「调用者没指定结果格式」。函数体里用一句 `choose` 把它替换成 `mid_fmt`：

```vhdl
constant r_fmt_c : FixFormat_t := choose(result_fmt = NullFixFormat_c, mid_fmt_c, result_fmt);
```

效果是：**不传 `result_fmt` ⇒ 输出就是全精度 `mid_fmt`，完全无损**；传一个更窄的 `result_fmt` ⇒ 触发舍入与饱和。这条「哨兵 → 回退到全精度」的约定，与 Python 侧 `r_fmt=None` 镜像（见 u5-l1、u4-l1）。

## 3. 本讲源码地图

本讲几乎全部内容都在同一个文件里：

| 文件 | 作用 |
|------|------|
| [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) | 主包：本讲的全部数学函数及其依赖的转换层都在这里 |
| hdl/en_cl_fix_private_pkg.vhd | 私有工具：本讲用到的 `to01`（把 `'H'`/`'1'` 归一为 `'1'`） |

本讲涉及的函数分布（行号均为当前 HEAD `e9123a9`）：

| 函数 | 行范围 | 类别 |
|------|--------|------|
| `resize_sensible` | 313–327 | 内部工具（乘法截断用） |
| `convert` | 329–351 | 内部工具（小数点对齐，u5-l2 详讲） |
| `union` | 353–360 | 内部工具（取格式超集） |
| `cl_fix_resize` | 1009–1022 | 公共转换（先 round 后 saturate，u5-l2 详讲） |
| `cl_fix_abs` / `cl_fix_neg` | 1111–1128 / 1130–1145 | 数学：符号运算 |
| `cl_fix_add` / `cl_fix_sub` / `cl_fix_addsub` | 1147–1170 / 1172–1195 / 1197–1212 | 数学：加减 |
| `cl_fix_mult` | 1214–1255 | 数学：乘法 |
| `cl_fix_shift` | 1257–1272 | 数学：移位 |
| `cl_fix_compare` / `cl_fix_sign` | 1274–1313 / 1315–1323 | 数学：比较与取符号 |

## 4. 核心概念与源码讲解

### 4.1 三段式模板：cl_fix_add / cl_fix_sub 与 signed 统一运算

#### 4.1.1 概念说明

`cl_fix_add` 与 `cl_fix_sub` 是整个数学函数层的「范本」。它们解决的问题是：两个格式可能完全不同的定点数 `a`（格式 `a_fmt`）和 `b`（格式 `b_fmt`）如何相加/相减，并把结果交给调用者指定的 `result_fmt`。

设计上，它们严格遵循三段式：**算 `mid_fmt` → 对齐运算 → resize**。这里有两个工程要点：

1. **运算统一走 `signed`**：即便两个输入都是无符号数，也先转成 `signed` 再相加。原因是补码运算位串等价（见 2.1），而统一类型能复用同一套 `numeric_std` 运算符，且规避了一个真实的综合器 bug（见 4.1.3）。
2. **`cl_fix_addsub` 只是分发器**：它多一个 `add : std_logic` 选择信号，运行时决定调 `cl_fix_add` 还是 `cl_fix_sub`。这样一条数据通路在综合后既能做加又能做减，但代价是要按「更宽的那一个」来分配位宽（`cl_fix_addsub_fmt = union(add_fmt, sub_fmt)`，见 U3）。

`cl_fix_abs` 和 `cl_fix_neg` 共用同一套模板，只是运算换成了取绝对值 / 取反，下面顺带覆盖。

#### 4.1.2 核心流程

`cl_fix_add` 的执行流程（`cl_fix_sub` 仅把 `+` 换成 `-`）：

```text
输入: a@a_fmt, b@b_fmt, result_fmt(可缺省), round, saturate

1. mid_fmt_c := cl_fix_add_fmt(a_fmt, b_fmt)      -- 综合期算出全精度中间格式
2. r_fmt_c   := choose(result_fmt=NullFixFormat_c, mid_fmt_c, result_fmt)
                                                   -- 哨兵回退: 缺省即 mid_fmt
3. a_v := convert(a, a_fmt, mid_fmt_c)             -- 把 a 对齐到 mid_fmt(小数点对齐+位宽补齐)
4. b_v := convert(b, b_fmt, mid_fmt_c)             -- 同上
5. mid_v := signed(a_v) + signed(b_v)              -- 统一 signed 做无损加法
6. return cl_fix_resize(mid_v, mid_fmt_c, r_fmt_c, round, saturate)
                                                   -- 收敛到目标格式(此处才舍入/饱和)
```

`convert`（u5-l2 详讲）只做**小数点对齐与位宽补齐**：低位不足补 0、高位不足做符号扩展或零扩展，绝不丢位，所以是无损的。真正的精度损失发生在第 6 步的 `cl_fix_resize`（先 round 后 saturate）。

`cl_fix_abs` / `cl_fix_neg` 的差异：`abs` 先用 `cl_fix_sign` 判断符号，为负则调 `cl_fix_neg` 取反，否则直接 `convert`；`neg` 则 `convert` 后做 `std_logic_vector(-signed(a_v))`。

#### 4.1.3 源码精读

`cl_fix_add` 的实现，注意第 1164–1168 行的注释和最后的统一 `signed` 加法：

[en_cl_fix_pkg.vhd:1147-1170](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1147-L1170) — `cl_fix_add` 完整体：算 `mid_fmt`、`choose` 处理哨兵、`convert` 两个输入、统一 `signed` 相加、`resize` 收敛。

关键的三句注释（行 1164–1167）解释了「为何无符号也走 signed」：

> Signed/unsigned addition/subtraction are identical when using two's complement. However, a long-standing Vivado bug causes incorrect post-synthesis behavior in DSP slices (pre-add or post-add) if `numeric_std.unsigned` is used. There are no known issues for `numeric_std.signed`, so we always use that.

即：理论上 signed/unsigned 加减位串相同，但 **Vivado 在 DSP slice 的预/后加法器里对 `unsigned` 有一个长期 bug**，会导致综合后行为错误；对 `signed` 没有问题。于是项目一律用 `signed`。这是 u8-l2 会专门梳理的「工具链 bug 规避」之一。

`cl_fix_sub` 与 `cl_fix_add` 结构**逐行对应**，只有运算符不同：

[en_cl_fix_pkg.vhd:1172-1195](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1172-L1195) — `cl_fix_sub`：同样的三段式，运算换成 `signed(a_v) - signed(b_v)`，并复制了同一段 Vivado DSP bug 注释。

`cl_fix_addsub` 只是个分发器：

[en_cl_fix_pkg.vhd:1197-1212](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1197-L1212) — `cl_fix_addsub`：用 `to01(add)` 把 `'1'`/`'H'` 都当作加法，否则做减法，分别转发给 `cl_fix_add` / `cl_fix_sub`。

这里的 `to01` 来自私有包，把 `'H'`（弱高电平）也归一成 `'1'`，避免选择信号里出现非 `'0'`/`'1'` 的元逻辑值导致分支异常：

[en_cl_fix_private_pkg.vhd:67-76](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_private_pkg.vhd#L67-L76) — `to01(std_logic)`：`'1'` 或 `'H'` 返回 `'1'`，其余返回 `'0'`。

同模板的符号运算 `cl_fix_neg`：

[en_cl_fix_pkg.vhd:1130-1145](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1130-L1145) — `cl_fix_neg`：`mid_fmt = cl_fix_neg_fmt(a_fmt)`，`convert` 后做 `-signed(a_v)`，再 `resize`。注意 `a_v` 和 `mid_v` 都是 `mid_fmt_c` 宽度的变量，所以 `convert` 先把输入对齐到 `mid_fmt`（可能比 `a_fmt` 宽，因为补码不对称，最小值取反需要多一位整数位，见 U3 的 `for_neg`）。

`cl_fix_abs` 借助 `cl_fix_sign` 选择是否取反：

[en_cl_fix_pkg.vhd:1111-1128](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1111-L1128) — `cl_fix_abs`：符号位为 1 时调 `cl_fix_neg`，否则直接 `convert`（已是非负数），最后都 `resize`。

#### 4.1.4 代码实践

**实践目标**：确认 `cl_fix_add` / `cl_fix_sub` 共享三段式模板，并理解哨兵回退。

**操作步骤**：

1. 打开 `hdl/en_cl_fix_pkg.vhd`，对照 4.1.3 的两个永久链接，把 `cl_fix_add` 与 `cl_fix_sub` 并排比较，圈出它们**唯一不同**的那一行（运算符）。
2. 在两段函数体里分别找到这三段：`mid_fmt_c` 的计算、两次 `convert`、最后的 `cl_fix_resize`。
3. 打开 `tb/cl_fix_add_tb.vhd`，找到例化 UUT 的位置，确认测试台是以随机/穷举数据驱动 `cl_fix_add`、再与 cosim 生成的黄金输出逐拍比对（验证闭环见 u7-l2）。

**需要观察的现象**：两个函数除运算符外其余逐行相同；`result_fmt` 缺省时，由于 `choose(...=NullFixFormat_c, mid_fmt_c, ...)`，`r_fmt_c == mid_fmt_c`，`resize` 退化为无损（同格式 round/saturate 都不动作）。

**预期结果**：你能用一句话写出三段式模板；能解释「不传 `result_fmt` ⇒ 无损返回」。

> 待本地验证：若已按 u1-l3 装好环境，可运行 `python sim/run.py --simulator=ghdl -v` 触发 `cl_fix_add` 的 cosim + 仿真，观察测试是否通过（本实践以源码阅读为主，运行步骤参考 u1-l3）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cl_fix_add` 对两个无符号输入也用 `signed(a_v) + signed(b_v)`，而不用 `unsigned(a_v) + unsigned(b_v)`？

**参考答案**：补码下加减法位串与无符号完全等价（见 2.1），结果位串对两种解释都正确；而统一用 `signed` 可以规避 Vivado 在 DSP slice 上对 `unsigned` 的长期综合 bug（见 1164–1167 行注释），同时复用同一套运算符。

**练习 2**：`cl_fix_addsub` 为什么要把结果格式预测成 `cl_fix_addsub_fmt = union(cl_fix_add_fmt, cl_fix_sub_fmt)`，而不是直接用 `cl_fix_add_fmt`？

**参考答案**：因为 `add` 信号在**运行时**才决定做加还是减，但位宽在**综合期**就钉死。减法可能让符号性翻转、需要额外的符号位（见 U3 的 `for_sub` 特殊处理）。取两者并集 `union` 才能保证无论运行时选哪个，位宽都够用。

---

### 4.2 cl_fix_mult：局部 "*" 重载与 resize_sensible

#### 4.2.1 概念说明

乘法比加法多两层麻烦：

1. **VHDL 标准没有定义混合类型的乘法**。`numeric_std` 只定义了 `signed * signed` 和 `unsigned * unsigned`，**没有** `signed * unsigned` 或 `unsigned * signed`。可是一个有符号数乘以一个无符号数是完全合法的定点运算，于是 `cl_fix_mult` 必须自己补上这两个重载。
2. **乘积位宽需要「明智地截断」**。两个 N 位、M 位的数相乘，全乘积是 N+M 位。但 `cl_fix_mult_fmt`（U3）在某些特殊情形下会预测出**比 N+M 更窄**的结果格式（例如两个 1 位有符号数相乘结果是无符号、幅值为 1 的操作数会让结果少 1 位）。此时需要把 N+M 位的乘积截到更窄，而 `numeric_std.resize` 在截断 `signed` 时会**保留符号位**（丢中间位），这会得到错误结果——必须用「直接截断高位」的 `resize_sensible`。

`resize_sensible` 就是专门为这种「格式预测已保证高位冗余」的截断场景而写的。

#### 4.2.2 核心流程

`cl_fix_mult` 的执行流程：

```text
输入: a@a_fmt, b@b_fmt, result_fmt, round, saturate

1. (局部) 定义 "*" 重载: signed*unsigned 与 unsigned*signed 都转成纯 signed 乘法
2. mid_fmt_c := cl_fix_mult_fmt(a_fmt, b_fmt)
3. r_fmt_c   := choose(result_fmt=NullFixFormat_c, mid_fmt_c, result_fmt)
4. 按 (a_fmt.S, b_fmt.S) 分四种情况做乘法:
     (0,0): unsigned * unsigned      -> 用 numeric_std.resize 对齐到 mid_width
     (0,1): unsigned * signed        -> resize_sensible 截到 mid_width
     (1,0): signed   * unsigned      -> resize_sensible 截到 mid_width
     (1,1): signed   * signed        -> resize_sensible 截到 mid_width
5. return cl_fix_resize(mid_v, mid_fmt_c, r_fmt_c, round, saturate)
```

`resize_sensible` 的核心逻辑：

```text
若 目标位宽 n >= 输入位宽:  用标准 resize(符号扩展/零扩展)
若 目标位宽 n <  输入位宽:  直接取低 n 位 a_c(n-1 downto 0)   -- 普通截断, 不保符号位
```

为什么普通截断是对的？因为 `cl_fix_mult_fmt` 的预测是**充分且必要**的（U3 的 `format_tests.py` 穷举证明）：它保证结果的所有有效信息都落在低 `mid_width` 位里，被截掉的高位要么是冗余的符号扩展、要么恒为常数。`numeric_std.resize` 的「保符号截断」反而会保留一个错误的旧符号位，所以必须绕开。

#### 4.2.3 源码精读

先看 `cl_fix_mult` 在函数体内部临时定义的两个 `*` 重载（行 1229–1237）：

[en_cl_fix_pkg.vhd:1214-1255](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1214-L1255) — `cl_fix_mult` 完整体：含局部 `*` 重载、`mid_fmt` 计算、四分支乘法、`resize`。

关键的局部重载（行 1229–1237）：

```vhdl
-- VHDL doesn't define a * operator for mixed signed*unsigned or unsigned*signed.
-- Just inside cl_fix_mult, it is safe to define them for local use.
function "*"(x : signed; y : unsigned) return signed is
begin
    return x * ('0' & signed(y));   -- 给无符号数前面补一个 0 符号位, 转成非负 signed
end function;

function "*"(x : unsigned; y : signed) return signed is
begin
    return y * x;                    -- 交换, 复用上一个重载
end function;
```

`('0' & signed(y))` 的含义：无符号数 `y` 一定是非负的，在它最高位前补一个 `'0'`（正号），重解释为 `signed`，数值不变。于是混合乘法被改写为纯 `signed * signed`，可直接用 `numeric_std` 的标准乘法。注释强调「只在 `cl_fix_mult` 内部这样定义是安全的」——因为这个补零语义只在「已知右操作数本就无符号」时成立，不宜提升为全局规则。

四分支乘法（行 1244–1252）：

- `(0,0)` 无符号乘无符号：用标准 `resize(unsigned*unsigned, mid_width)`。无符号 `resize` 截断时本就是取低位，与普通截断一致，所以不必用 `resize_sensible`。
- 其余三种含符号数的情形：一律 `resize_sensible(...)`，确保需要截断时走「普通截断」而非 `numeric_std` 的保符号截断。

再看 `resize_sensible` 本体：

[en_cl_fix_pkg.vhd:313-327](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L313-L327) — `resize_sensible`：增长用标准 `resize`（符号扩展），截断用 `a_c(n-1 downto 0)`（普通截断，注释说明这比 `numeric_std.resize` 的保符号截断更合理）。

#### 4.2.4 代码实践

**实践目标**：解释 `cl_fix_mult` 为何要局部定义 `signed * unsigned`，并理解 `resize_sensible` 在截断中的作用。

**操作步骤**：

1. 打开 [en_cl_fix_pkg.vhd:1229-1237](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1229-L1237)，在草稿纸上模拟 `('0' & signed(y))`：取一个 3 位无符号数 `y = "111"`（=7），补零得 `"0111"`，作为 signed 仍是 +7，确认数值不变。
2. 思考边界情形：`a_fmt = (1,1,0)`（1 位有符号，取值 {-1,0}）与 `b_fmt = (1,1,0)` 相乘，按 U3 的 `for_mult`，结果格式应为 `(0,1,0)`（无符号 1 位，取值 {0,1}）。全乘积是 2 位 `signed*signed`，但 `mid_width` 只有 1 位，于是 `resize_sensible` 会截掉高位——确认被截掉的那一位在「结果恒非负」下是冗余的符号扩展。
3. （可选）打开 `tb/cl_fix_mult_tb.vhd`，找到它驱动的格式组合，确认测试覆盖了含符号×无符号的情形。

**需要观察的现象**：局部 `*` 重载把混合乘法变成纯 signed 乘法；`resize_sensible` 在乘积位宽 > `mid_width` 时丢弃高位，而这之所以正确，依赖 `cl_fix_mult_fmt` 的「充分且必要」保证。

**预期结果**：你能写出两句话——① 局部重载是因为 VHDL 标准没有 `signed*unsigned`；② 用 `resize_sensible` 是为了避免 `numeric_std.resize` 截断 signed 时保符号位导致错误。

> 待本地验证：乘积截断的正确性由 `bittrue/tests/python/format_tests.py` 的穷举断言在 Python 参考模型上保证，再经 cosim 传到 HDL 测试台比对（见 u8-l3、u7-l2）。

#### 4.2.5 小练习与答案

**练习 1**：为什么局部重载 `function "*"(x : signed; y : unsigned)` 里要写成 `x * ('0' & signed(y))`，而不是直接 `x * signed(y)`？

**参考答案**：直接 `signed(y)` 会把 `y` 的最高位当成符号位，可能把一个本应为正的无符号数解释成负数。在前面补一个 `'0'` 作为新的符号位，原最高位变成普通数值位，数值保持非负不变，语义才正确。

**练习 2**：若把四分支里 `(1,1)` 情形的 `resize_sensible` 换成 `numeric_std.resize`，在什么情况下会出错？

**参考答案**：当 `cl_fix_mult_fmt` 预测的 `mid_width` 小于全乘积位宽（即存在「-1 位」特殊情形，如两个 1 位有符号相乘得无符号 1 位）时，`numeric_std.resize` 截断 `signed` 会**保留原符号位、丢弃中间位**，导致结果的高位语义错误；而 `resize_sensible` 取低 `mid_width` 位才是格式预测所保证的正确结果。

---

### 4.3 cl_fix_shift：借 dummy_fmt 实现无损移位

#### 4.3.1 概念说明

`cl_fix_shift(a, a_fmt, shift, result_fmt, ...)` 的语义是：把定点数 `a` 乘以 \(2^{\text{shift}}\)（`shift` 为正左移、为负右移），再收敛到 `result_fmt`。关键约束是——**移位这一步本身必须无损**，精度损失只允许发生在随后的 resize。

实现这一点的技巧非常优雅：**根本不做任何比特移动，只改格式标注**。

回忆定点表示：同一个比特串，把它解释成格式 `(S,I,F)` 时，其 LSB 权重是 \(2^{-F}\)。如果我们**保持比特串不变、只把 F 改一改**，数值就乘除了一个 2 的幂。`cl_fix_shift` 正是利用这一点：它构造一个「障眼法格式」`dummy_fmt`，让 `cl_fix_resize` 把 `a` 对齐到 `dummy_fmt`（这一步只是小数点对齐、无损），然后由调用者把输出按 `result_fmt` 解释——两次解释之间的 F 差恰好就是移位量。

#### 4.3.2 核心流程

设 `result_fmt = (S, I, F)`，移位量 `shift`。构造：

\[
\text{dummy\_fmt} = (S,\ I - \text{shift},\ F + \text{shift})
\]

注意三点：

- `dummy_fmt.S = result_fmt.S`（符号性不变）。
- `dummy_fmt` 的总位宽 = \(S + (I-\text{shift}) + (F+\text{shift}) = S+I+F\)，**与 `result_fmt` 宽度完全相同**。

然后执行：

```text
return cl_fix_resize(a, a_fmt, dummy_fmt, round, saturate)
```

为什么这就完成了移位？设输出比特串为 \(B\)：

- \(B\) 解释为 `dummy_fmt`（其 \(F' = F + \text{shift}\)）时，数值 \(= \text{int}(B) \cdot 2^{-(F+\text{shift})}\)；而 `cl_fix_resize` 保证这个值等于原值 \(v\)。
- \(B\) 解释为 `result_fmt`（其 \(F\)）时，数值 \(= \text{int}(B) \cdot 2^{-F} = v \cdot 2^{\text{shift}}\)。

也就是说，**输出比特串按 `result_fmt` 解释，正好是原值乘以 \(2^{\text{shift}}\)**。移位纯粹来自两次格式解释之间的小数点位置差。而 `cl_fix_resize` 内部的 round（舍掉 `a_fmt.F - (F+shift)` 位小数）与 saturate（限制到 `result_fmt` 范围）就顺带完成了精度收敛。

对照 `cl_fix_shift_fmt`（格式预测版本，U3）：移位后的格式应是 `(a_fmt.S, a_fmt.I + shift, a_fmt.F - shift)`，与上面 `dummy_fmt` 的方向正好「镜像」——一个是描述移位后数据的格式，一个是用来骗 `resize` 对齐的目标格式，二者通过移位量关联。

#### 4.3.3 源码精读

`cl_fix_shift` 的实现极其简短，核心只有一行构造 `dummy_fmt`：

[en_cl_fix_pkg.vhd:1257-1272](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1257-L1272) — `cl_fix_shift`：构造 `dummy_fmt_c = (result_fmt.S, result_fmt.I - shift, result_fmt.F + shift)`，然后直接 `cl_fix_resize(a, a_fmt, dummy_fmt_c, ...)`。注释明确说明这是无损移位（等价于 `*2.0**shift`），且 `shift` 方向为左（负数右移）。

注意 `cl_fix_shift` 的 `result_fmt` **没有默认值**（包头第 251 行无 `:= NullFixFormat_c`），因为移位必须知道目标格式才能确定 `dummy_fmt`。这与其它数学函数不同（见 u5-l1 的「两个破例」）。

它依赖的 `cl_fix_resize`：

[en_cl_fix_pkg.vhd:1009-1022](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1009-L1022) — `cl_fix_resize`：先按 `round` 算出 `rounded_fmt` 并 `cl_fix_round`，再 `cl_fix_saturate` 到 `result_fmt`（即 dummy_fmt）。固定「先 round 后 saturate」（u5-l2 详讲）。

以及负责小数点对齐的 `convert`（resize 内部的 round 会用到它）：

[en_cl_fix_pkg.vhd:329-351](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L329-L351) — `convert`：按 `offset_c = rFmt.F - aFmt.F` 做二进制小数点对齐，符号扩展高位、低位补零，绝不减少小数位（`offset_c` 为 `natural` 类型，编译期挡住非法用法）。

对照格式预测版 `cl_fix_shift_fmt`：

[en_cl_fix_pkg.vhd:596-606](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L596-L606) — `cl_fix_shift_fmt`：返回 `(a_fmt.S, a_fmt.I + max_shift, a_fmt.F - min_shift)`，描述移位后数据的格式，与 `cl_fix_shift` 内部的 `dummy_fmt` 方向相反、用途不同。

#### 4.3.4 代码实践

**实践目标**：用具体数字验证「改格式标注 = 移位」。

**操作步骤**：

设 `a_fmt = (0,7,0)`（无符号 8 位整数），`shift = 1`（左移一位，乘 2），`result_fmt = (0,8,0)`（无符号 9 位）。

1. 按 `cl_fix_shift` 的公式算 `dummy_fmt = (0, 8-1, 0+1) = (0,7,1)`，总宽 \(0+7+1=8\)，与 `result_fmt` 宽度 9 不同？请复核：`result_fmt` 宽 = \(0+8+0=8\)，`dummy_fmt` 宽 = \(0+7+1=8\)，二者都是 8 位 ✓。
2. 取 `a = "00000011"`（=3）。`cl_fix_resize(a, (0,7,0), (0,7,1))` 把它对齐到 `(0,7,1)`：小数点右移 1 位等价于数值 `3` 在 `(0,7,1)` 下表示为 `011.0` 即 `"00000110"`。
3. 调用者把输出 `"00000110"` 按 `result_fmt=(0,8,0)` 解释：整数 = 6 = \(3 \times 2^1\) ✓，移位成功。

**需要观察的现象**：比特串从 `"00000011"` 变成 `"00000110"`，看起来像「左移一位」，但函数内部并没有显式移位操作——它只是 `resize` 到了一个 F 多 1 的格式，移位效果来自重新解释。

**预期结果**：你能解释 `dummy_fmt` 与 `result_fmt` 宽度为何必然相等，以及为何移位无损（resize 到 dummy_fmt 时 round 不丢位，因为 dummy_fmt.F 比 a_fmt.F 大）。

> 待本地验证：上述手算可对照 `tb/cl_fix_shift_tb.vhd` 的测试用例核对。

#### 4.3.5 小练习与答案

**练习 1**：`cl_fix_shift` 为什么不像其它数学函数那样给 `result_fmt` 一个 `NullFixFormat_c` 默认值？

**参考答案**：因为 `dummy_fmt` 是由 `result_fmt` 直接构造的（`result_fmt.I - shift` 等），没有 `result_fmt` 就无法定义 `dummy_fmt`，移位也就无法落到任何具体格式上。所以 `result_fmt` 是必填参数。

**练习 2**：若 `shift = -1`（右移一位，除以 2），`dummy_fmt` 的 F 会变大还是变小？这意味着 round 阶段会丢位吗？

**参考答案**：`dummy_fmt.F = result_fmt.F + (-1) = result_fmt.F - 1`，F 变小。若 `dummy_fmt.F < a_fmt.F`，则 `cl_fix_resize` 内部的 round 阶段需要减少小数位，**会丢位**（按 `round` 模式舍入）。这是右移必然带来的精度损失，发生在 resize 而非「移位」本身——移位（格式重标注）仍是无损的，损失来自随后收敛到更窄格式。

---

### 4.4 cl_fix_compare 与 cl_fix_sign：对齐比较与符号提取

#### 4.4.1 概念说明

`cl_fix_compare` 解决「两个格式不同的定点数如何比较大小」。难点是：它们的二进制小数点位置不同、位宽不同、甚至符号性不同，不能直接比比特串。

思路仍然是「先对齐，再比较」：用 `union` 取两个格式的**最小公共超集**（S/I/F 各取最大），把两边都 `convert` 到这个公共格式，于是数值对齐、位宽一致，就能用 `numeric_std` 的比较运算符直接比了。

比较时按 `mid_fmt.S` 决定用 `signed` 还是 `unsigned` 运算符——这很关键：若公共格式是无符号（两边都无符号），用 `signed` 比较会把最高位当符号位而判错。

`cl_fix_sign` 则是取出一个定点数的符号位：有符号且非空时返回最高位，否则（无符号或 0 位宽）返回 `'0'`。它被 `cl_fix_abs` 和舍入偏移逻辑（`get_unit_bit` 等）复用。

#### 4.4.2 核心流程

`cl_fix_compare(comparison, a, aFmt, b, bFmt)` 的流程：

```text
1. mid_fmt_c := union(aFmt, bFmt)            -- S/I/F 各取最大, 得最小公共超集
2. a_v := convert(a, aFmt, mid_fmt_c)        -- 对齐 a
3. b_v := convert(b, bFmt, mid_fmt_c)        -- 对齐 b
4. 若 mid_fmt_c.S = 1: 用 signed 比较 (a_v op b_v)
   否则:           用 unsigned 比较
   其中 op 由字符串 comparison 选择: "=" "!=" "<" ">" "<=" ">="
   无法识别的 comparison -> report Failure
```

`cl_fix_sign(a, aFmt)` 的流程：

```text
若 aFmt.S = 0 或 cl_fix_width(aFmt) = 0:  返回 '0'   -- 无符号或空, 恒非负
否则:                                       返回最高位 a_c(a_c'high)
```

#### 4.4.3 源码精读

`cl_fix_compare`：

[en_cl_fix_pkg.vhd:1274-1313](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1274-L1313) — `cl_fix_compare`：`union` 对齐两边，按 `mid_fmt_c.S` 分 `signed`/`unsigned` 两套六种比较，无法识别的 `comparison` 串触发 `report ... severity Failure`。

注意它用字符串 `comparison` 选择运算符，而不是枚举——这是为了调用书写方便（如 `cl_fix_compare("<", a, aFmt, b, bFmt)`），代价是无法在编译期挡住拼写错误，只能运行时报错。

它依赖的 `union`：

[en_cl_fix_pkg.vhd:353-360](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L353-L360) — `union`：对 S、I、F 各取 `maximum`，得到能同时容纳两个格式的最小格式。

`cl_fix_sign`：

[en_cl_fix_pkg.vhd:1315-1323](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1315-L1323) — `cl_fix_sign`：先把输入强制成 `downto 0` 范围（`a_c`），无符号或 0 位宽返回 `'0'`，否则返回最高位。注意它处理了 `cl_fix_width(aFmt) = 0` 的退化情形，避免越界访问。

#### 4.4.4 代码实践

**实践目标**：跟踪一次「格式不同」的比较，理解 `union` 对齐的必要性。

**操作步骤**：

设 `aFmt = (0,4,0)`（无符号 4 位，范围 0–15），`bFmt = (1,3,0)`（有符号 4 位，范围 -8–7）。

1. 算 `union(aFmt, bFmt)`：S=max(0,1)=1，I=max(4,3)=4，F=max(0,0)=0，得 `mid_fmt = (1,4,0)`（有符号 5 位，范围 -16–15，能容纳两者）。
2. 取 `a = "1000"`（无符号 = 8），`b = "1000"`（有符号补码 = -8）。直接比比特串会误判相等。
3. `convert(a, (0,4,0), (1,4,0))` 把 8 符号扩展成 5 位 `"01000"`；`convert(b, (1,3,0), (1,4,0))` 把 -8 符号扩展成 5 位 `"11000"`。
4. `mid_fmt.S = 1`，用 `signed` 比较：`signed("01000") = +8 > signed("11000") = -8`，结论 `a > b` ✓。

**需要观察的现象**：原始比特串都是 `"1000"`，但经 `union` + `convert` 对齐后变成不同符号扩展，`signed` 比较给出正确大小关系。若误用 `unsigned` 比较，两个 `"1000"` 会被判成相等——这正是 `cl_fix_compare` 要按 `mid_fmt.S` 分支的原因。

**预期结果**：你能解释为何 `cl_fix_compare` 必须先 `union` 再比较，以及为何按 `mid_fmt.S` 选择 signed/unsigned。

> 待本地验证：可对照 `tb/cl_fix_compare_tb.vhd` 的测试用例。

#### 4.4.5 小练习与答案

**练习 1**：`cl_fix_compare` 为什么要在比较前先 `convert` 到 `union(aFmt, bFmt)`，而不是直接比 `a` 和 `b` 的比特串？

**参考答案**：两个格式不同的定点数，其比特串的二进制小数点位置、位宽、符号性都不同，直接比比特串没有意义。`union` 取最小公共超集后 `convert`，能把两边对齐到同一格式（数值不变、位宽一致、符号性统一），此时比特串的比较才等价于数值比较。

**练习 2**：`cl_fix_sign` 为什么要特别处理 `cl_fix_width(aFmt) = 0` 的情形？

**参考答案**：0 位宽的格式没有任何比特可访问。若直接取 `a_c(a_c'high)`，在空范围上会越界或产生未定义行为。对这种退化情形统一返回 `'0'`（视为非负），既安全又语义合理（0 位格式只能表示常数 0）。

---

## 5. 综合实践：用三段式搭建一条「乘→加」数据通路

把本讲四个模块串起来，设计一条定点数据通路的草图（纸面练习，无需综合）。

**场景**：计算 \(y = (a \times b) + c\)，其中：

- `a_fmt = (1,1,7)`、`b_fmt = (1,1,7)`、`c_fmt = (1,8,0)`。
- 要求最终输出 `result_fmt = (1,8,0)`，舍入 `NonSymPos_s`，饱和 `SatWarn_s`。

**任务**：

1. **乘法级**：写出 `cl_fix_mult` 的 `mid_fmt = cl_fix_mult_fmt(a_fmt, b_fmt)`（参考 U3：两个 1 位有符号相乘结果为无符号 1 位，故 `mid_fmt = (0,2,14)`）。说明这一步用到了 4.2 的局部 `*` 重载和 `resize_sensible` 的哪一种情形（应为 `(1,1)` 即 signed*signed）。
2. **加法级**：把乘积格式 `(0,2,14)` 与 `c_fmt = (1,8,0)` 喂给 `cl_fix_add`，写出它的 `mid_fmt = cl_fix_add_fmt((0,2,14),(1,8,0))`。说明这一步遵循 4.1 的三段式（`convert` 两边 → `signed` 相加 → `resize`），并指出加法统一用 `signed` 规避了哪个工具链 bug（Vivado DSP）。
3. **收敛**：最后一级 `cl_fix_resize` 把全精度结果收敛到 `(1,8,0)`，按 4.3 回顾的 `cl_fix_resize = 先 round 后 saturate`，说明为何顺序不可交换（饱和要求 F 不变，必须先 round 对齐小数位）。
4. **边带**：若用 U6 的可流水线化组件实现，说明 `meta_width_g` 如何让边带信号（如 valid/last）贯穿乘法、加法两级寄存器而不丢失。

**预期成果**：一张标注了每级 `mid_fmt`、`result_fmt`、所用 `cl_fix_*` 函数与寄存器模式的数据通路草图，能清楚说明三段式在每一级如何复用、精度损失只发生在哪些 `resize`。

> 待本地验证：格式推导可用 Python 参考模型 `FixFormat.for_mult` / `for_add`（U4）核对；端到端数值正确性由 cosim + VUnit 测试台保证（U7）。

## 6. 本讲小结

- `cl_fix_add` / `cl_fix_sub` / `cl_fix_abs` / `cl_fix_neg` 共享同一套三段式模板：**算 `mid_fmt` → `convert` 对齐 → 无损运算 → `cl_fix_resize` 收敛**，精度损失只发生在最后的 resize。
- 加减法**统一用 `signed`** 运算，原因是补码位串等价，且能规避 Vivado 在 DSP slice 上对 `unsigned` 的长期综合 bug。
- `cl_fix_addsub` 只是按 `to01(add)` 在 `cl_fix_add` / `cl_fix_sub` 之间分发，位宽取 `union(add_fmt, sub_fmt)` 以兼容运行时选择。
- `cl_fix_mult` 在函数体内部临时定义 `signed * unsigned` 重载（用 `('0' & signed(y))` 补零转纯 signed），因为 VHDL 标准未提供混合类型乘法；含符号的乘积用 `resize_sensible` 做**普通截断**而非 `numeric_std` 的保符号截断。
- `cl_fix_shift` **不做任何比特移动**，只构造 `dummy_fmt = (result_fmt.S, result_fmt.I-shift, result_fmt.F+shift)` 并 `resize` 到它，移位效果来自输出比特串在 `dummy_fmt` 与 `result_fmt` 之间的格式重解释，移位本身无损。
- `cl_fix_compare` 先 `union` 两个格式再 `convert` 对齐，按公共格式的符号性选择 signed/unsigned 比较运算符；`cl_fix_sign` 取最高位作符号，并安全处理 0 位宽退化情形。

## 7. 下一步学习建议

- **U6（可流水线化组件）**：本讲的数学函数是纯组合逻辑；U6 会把它们包成带 `clk/rst/valid/meta` 接口、可按 `RegisterMode_t` 自动插寄存器的实体（`en_cl_fix_round/saturate/resize.vhd`），是把本讲函数落到时序通路的关键。
- **U7（协同仿真验证）**：本讲多次提到「正确性由 cosim + 测试台保证」，U7 会讲清 `bittrue/cosim/` 如何用 Python 参考模型生成黄金数据、`tb/` 如何逐拍比对、`sim/run.py` 如何装配。
- **U8（工具链 bug 规避）**：本讲遇到的「Vivado DSP unsigned bug」「自实现 resize_sensible 截断」都属于工程化规避，U8-l2 会系统梳理源码中所有这类 workaround。
- **继续阅读源码**：建议通读 `hdl/en_cl_fix_pkg.vhd` 第 1111–1323 行全部数学函数体，并对照 `tb/cl_fix_add_tb.vhd`、`tb/cl_fix_mult_tb.vhd`、`tb/cl_fix_shift_tb.vhd`、`tb/cl_fix_compare_tb.vhd` 理解测试如何驱动这些函数。

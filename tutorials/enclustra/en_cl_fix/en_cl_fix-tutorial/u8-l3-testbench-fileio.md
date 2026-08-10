# VHDL testbench 模式与文件 I/O

## 1. 本讲目标

本讲是验证单元（单元 8）的收尾课，把 u8-l1（Python 生成黄金参考）和 u8-l2（VUnit 调度）连成闭环的最后一段补上：**VHDL testbench 如何消费 cosim 写出的文件、重生成输入、调用待测对象并逐位对拍**。

学完后你应该能：

- 说出 en_cl_fix 里两种 testbench 写法（函数级对拍、RTL 例化对拍）各自的适用场景与结构差异。
- 读懂 `cl_fix_add_tb.vhd` / `cl_fix_round_tb.vhd` 里「读格式 → 重生成输入 → 调用 → 比对」的主流程，并指出每一步对应的文件 I/O 调用点。
- 理解 `en_cl_fix_fileio_pkg` 如何把 en_cl_fix 的 `FixFormat_t` 包装成 en_tb 通用的文件读写接口，以及它为何要做「符号位（Signedness）转换」。
- 理解 en_tb 库的定位、context 引用方式，以及它在本项目验证链路中的边界。

## 2. 前置知识

本讲默认你已经掌握：

- **定点格式 [S, I, F]** 与 `FixFormat_t`、`FixRound_t`、`FixSaturate_t` 三大类型（u2-l1、u2-l3）。
- **三段式算术骨架**（mid_fmt → 运算 → resize）与 round/saturate/resize 的语义（单元 4、单元 5）。
- **cosim 验证思想**：Python 参考模型算黄金参考、写盘，VHDL 仿真读盘逐位对拍（u8-l1）。
- **VUnit 调度**：`pre_config` 钩子在仿真前触发 cosim 的 `run()` 写出 `data/` 文件，`cosim_runner` 保证每轮只跑一次（u8-l2）。

两个本讲要用到的 VHDL 语法点，先在此温习：

- **枚举的位置编码往返**。VHDL 中离散类型有「位置号（position）」：`FixRound_t'val(n)` 把位置号 `n` 转回枚举值，`'pos` 反向。cosim 的 Python 侧用枚举的 `.value`（即位置号）写成整数文件 `rnd.txt` / `sat.txt`，VHDL 侧用 `'val` 读回（u8-l1 提到的「枚举码往返」契约）。
- **标识符大小写不敏感**。VHDL 里 `datapath_c` 与 `DataPath_c` 是同一个标识符，阅读 `cl_fix_round_tb.vhd` 时若看到大小写不一致不必困惑。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [tb/cl_fix_add_tb.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd) | **函数级对拍** testbench：不例化任何 DUT，直接调用 `cl_fix_add` 函数，与文件里的期望逐位比对。本讲「主流程」范本。 |
| [tb/cl_fix_round_tb.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd) | **RTL 例化对拍** testbench：用 `generate` 为每个用例例化 `en_cl_fix_round` 实体，带握手与 meta 旁路。本讲代码实践的阅读对象。 |
| [tb/util/en_cl_fix_fileio_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/util/en_cl_fix_fileio_pkg.vhd) | 文件 I/O 包装包：把 en_cl_fix 的 `FixFormat_t` 适配到 en_tb 的通用文本读写。 |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd) | 提供 `FixFormatArray_t`、`to_string(FixFormat_t)`、`cl_fix_format_from_string` 等被 fileio 包复用的工具。 |
| [lib/en_tb/README.md](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/lib/en_tb/README.md) / [lib/en_tb/doc/index.md](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/lib/en_tb/doc/index.md) / [lib/en_tb/doc/en_tb_fileio_context/readme.md](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/lib/en_tb/doc/en_tb_fileio_context/readme.md) | en_tb 库定位、context 说明与依赖关系。 |
| [lib/en_tb/hdl/en_tb_fileio_text_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/lib/en_tb/hdl/en_tb_fileio_text_pkg.vhd) / [lib/en_tb/hdl/en_tb_base_pkg.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/lib/en_tb/hdl/en_tb_base_pkg.vhd) | 被包装的底层：通用文本读写（`read_file`/`read`/`write`）与基础类型（`Signedness_t`、`SlvArray_t`）。 |

> 提示：`bittrue/cosim/<op>/data/` 目录在仓库里通常是**空的**——这些 `*_fmt.txt`、`rnd.txt`、`sat.txt`、`test{N}_output.txt` 文件是 cosim 脚本在 VUnit 的 `pre_config` 阶段**运行时生成**的（见 u8-l1、u8-l2）。直接 `git ls-files` 看不到它们是正常的。

## 4. 核心概念与源码讲解

### 4.1 testbench 文件 I/O 验证总览

#### 4.1.1 概念说明

u8-l1 已经讲过 cosim 的 Python 侧如何「穷举格式/模式 → 算黄金参考 → 写盘」。本讲讲它的对偶：**VHDL 侧如何把这些文件读回来、重做同样的运算、并比对**。

这里有一个贯穿全项目验证设计的核心取舍——**「只存输出，不存输入」**。对于一个 `[0,2,2]` 的小格式，输入只有 16 种取值，存起来无所谓；但对于 `[1,8,16]` 这样的格式，输入多达上亿种组合，全存进文件既慢又臃肿。于是 cosim 选择：

- **输出**写成文件（`test{N}_output.txt`），因为输出是「待验证的事实」，必须固化。
- **输入**不存文件，而是**在 Python 和 VHDL 两侧用同一套计数规则各自重新生成**——两侧都按 `b 外 a 内` 的嵌套计数器遍历全部取值，顺序一致即可对齐。

这把「输入数据的传递」从「文件搬运」变成了「算法复现」，是个很漂亮的去耦。代价是两侧的遍历顺序**必须严格一致**（u8-l1 强调的隐式契约之一），否则同一个下标 `Idx_v` 在两侧指向的就是不同的输入，对拍就会假错。

#### 4.1.2 核心流程

en_cl_fix 的 testbench 有两种写法，结构不同但都遵循同一个四步骨架：

```
┌─────────────────────────────────────────────────────────────┐
│  1. 读配置：cl_fix_read_format_file(a_fmt/r_fmt)            │
│            read_file(rnd/sat) → integer_vector              │
│  2. 重生成输入：按 [min,max] 计数器遍历全部取值              │
│  3. 调用待测对象（函数 or RTL 实体）                         │
│  4. 比对：cl_fix_read_file(test{N}_output) → Expected(Idx)   │
└─────────────────────────────────────────────────────────────┘
```

两种写法的区别只在第 3 步：

| 维度 | 函数级对拍（`cl_fix_add_tb`） | RTL 例化对拍（`cl_fix_round_tb`） |
|------|------------------------------|-----------------------------------|
| 待测对象 | 库函数 `cl_fix_add(...)` | RTL 实体 `en_cl_fix_round` |
| 时钟 | 几乎不需要（仅防仿真迭代上限） | 必需，驱动握手 |
| 验证内容 | 算法本身的位级正确性 | RTL 组件（含可选寄存器、握手、meta） |
| 结构 | 一个 `Check(i)` 过程 + 单进程 | `generate` 每用例一组 input/UUT/check 进程 |

为什么要两种？因为库同时提供**纯函数**（`en_cl_fix_pkg`，可综合）和**RTL 组件**（`en_cl_fix_round.vhd` 等，见 u7-l1）。前者要验证「算法对不对」，后者要验证「封装成硬件后行为对不对、握手/meta 对不对」。两者都用同一批 cosim 黄金参考，于是**算法 bug 和封装 bug 被分到两层各自暴露**。

#### 4.1.3 源码精读

先看两个 testbench 顶部的库声明，它们揭示了依赖关系。`cl_fix_add_tb.vhd` 的声明区：

[tb/cl_fix_add_tb.vhd:L31-L36](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd#L31-L36) —— 引入 en_tb 的 fileio context、本库的 `en_cl_fix_pkg` 与 `en_cl_fix_fileio_pkg`。

注意三层关系：`en_tb`（通用 testbench 库）→ `en_cl_fix_fileio_pkg`（本项目对它的定点专用包装）→ `cl_fix_*_tb`（具体用例）。本讲 4.4、4.5 会分别拆开下两层。

#### 4.1.4 代码实践

**实践目标**：在动手读细节前，先建立「文件即契约」的直觉。

**操作步骤**：

1. 打开 [tb/cl_fix_add_tb.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd) 与 [tb/cl_fix_round_tb.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd)。
2. 只看每个文件顶部的 `constant ... := ... read_format_file/read_file(...)` 行，列出它们各自读了哪几个文件。
3. 对照 u8-l1 列出的 `data/` 文件约定（`a_fmt.txt` / `b_fmt.txt` / `r_fmt.txt` / `rnd.txt` / `sat.txt` / `test{N}_output.txt`），确认两边对得上。

**预期结果**：`cl_fix_add_tb` 读 5 个文件（a/b/r_fmt + rnd + sat），`cl_fix_round_tb` 读 3 个（a/r_fmt + rnd，因为 round 不涉及 saturation）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 cosim 不把输入也写成文件？
**答**：输入组合数可能极巨大（如 `[1,8,16]` 上亿种），存文件慢且臃肿；改为两侧用同一计数规则各自重生成，把「数据传递」变成「算法复现」，只要顺序一致即可对齐。

**练习 2**：函数级 testbench 几乎不需要时钟，为什么源码里还是写了 `wait until rising_edge(Clk)`？
**答**：注释里说得很直白——为了避免仿真器的迭代次数上限（iteration limits），并避免开发者看到时间一直停在 0 ns 产生困惑；它对算法结果没有任何影响。

---

### 4.2 cl_fix_add_tb：函数级对拍与 Check 流程

#### 4.2.1 概念说明

`cl_fix_add_tb` 是最纯粹的「算法回归测试」：它**不例化任何硬件**，直接在 testbench 进程里调用 `en_cl_fix_pkg` 里的 `cl_fix_add` 函数，把返回值和 cosim 写好的期望逐位比对。它的存在意义是——一旦 Python 模型与 VHDL 函数出现任何不一致（哪怕一个舍入位），这里立刻失败。

注意它测的是**库函数**，不是 RTL 实体。RTL 实体的验证由 `cl_fix_round_tb` 这一类承担。

#### 4.2.2 核心流程

```
对每个用例 i = 0 .. TestCount_c-1：        ← 每行对应一个 (a_fmt,b_fmt,r_fmt,rnd,sat)
    1. 读期望：Expected_c := cl_fix_read_file("test{i}_output.txt", r_fmt(i))
    2. 由格式算整数取值范围：Amin..Amax, Bmin..Bmax
    3. 双层计数器遍历（b 外、a 内）：
         for b in Bmin..Bmax:
           for a in Amin..Amax:
              Result := cl_fix_add(from_integer(a,a_fmt), from_integer(b,b_fmt), ...)
              比对 Result == Expected_c(Idx_v)     ← Idx_v 与 cosim 同序递增
              Idx_v += 1
```

这里的 `for b ... for a ...` 嵌套顺序是**刻意写成 b 外 a 内**的——必须与 cosim 的 Python 侧（`repeat_whole_array` 在 b、`repeat_each_value` 在 a，u8-l1）逐一对齐，否则 `Expected_c(Idx_v)` 的下标就会错位。

#### 4.2.3 源码精读

**配置读取**（架构声明区，编译期常量）：

[tb/cl_fix_add_tb.vhd:L52-L63](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd#L52-L63) —— `DataPath_c` 指向 cosim 的 `data/` 目录；格式文件用 `cl_fix_read_format_file` 读成 `FixFormatArray_t`，舍入/饱和用 en_tb 原生的 `read_file` 读成 `integer_vector`。`TestCount_c := AFmt_c'length` 以格式数组长度作为用例总数。

> 这里有个值得注意的细节：`rnd.txt` / `sat.txt` 是**普通整数**（枚举的位置号），所以直接用 en_tb 的 `read_file(...) return integer_vector`，**不经过** `en_cl_fix_fileio_pkg` 包装；只有定点数据与 `FixFormat_t` 才需要包装（见 4.4）。

**期望与取值范围**：

[tb/cl_fix_add_tb.vhd:L73-L81](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd#L73-L81) —— `Check(i)` 过程里，先用 `cl_fix_read_file` 把第 `i` 个用例的期望读成 `SlvArray_t`；再用 `cl_fix_min_value`/`cl_fix_max_value` + `cl_fix_to_integer` 把每个格式的取值范围换算成整数计数区间。这正是「重生成输入」所需的边界。

**调用与比对**：

[tb/cl_fix_add_tb.vhd:L85-L102](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd#L85-L102) —— 双层计数器 `for b ... for a ...`（b 外 a 内）；每步用 `cl_fix_from_integer` 把整数还原成定点比特，调 `cl_fix_add`，再与 `Expected_c(Idx_v)` 比对。不等时先用 `Str(...)` 打印人类可读的实数值与格式，再 `check_equal` 报错。

`Str` 这个本地辅助函数很巧妙：

[tb/cl_fix_add_tb.vhd:L68-L71](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd#L68-L71) —— 把整数 `x` 在格式 `XFmt` 下的实数值转成可读字符串（`from_integer` → `to_real` → `to_string`），让失败信息一眼能看懂「是 3.5 + 2.25 算错了」，而不是一堆裸比特。

#### 4.2.4 代码实践

**实践目标**：理解「枚举码往返」如何把 Python 的舍入/饱和模式送进 VHDL。

**操作步骤**：

1. 在 [tb/cl_fix_add_tb.vhd:L60-L61](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd#L60-L61) 看到 `Rnd_c` / `Sat_c` 是整数数组。
2. 在 [tb/cl_fix_add_tb.vhd:L88-L92](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_add_tb.vhd#L88-L92) 看到调用处用 `FixRound_t'val(Rnd_c(i))`、`FixSaturate_t'val(Sat_c(i))` 把整数还原成枚举。
3. 回顾 u8-l1：Python 侧用 `FixRound.XXX.value`（位置号）写盘。两侧的位置号定义必须一致（`FixRound_t` 与 Python `FixRound` 的枚举顺序逐字对应，u2-l1/u2-l3）。

**预期结果**：你能口头复述「Python 写 `.value` 整数 → 文件 → VHDL 用 `'val` 读回」这条往返链路，并解释为什么两侧枚举顺序不能打乱。

#### 4.2.5 小练习与答案

**练习 1**：`Check(i)` 里 `Idx_v` 为什么必须从 0 开始、按 `b 外 a 内` 递增？
**答**：因为 cosim 的 Python 侧用 `repeat_whole_array`（在 b）配 `repeat_each_value`（在 a）生成笛卡尔积，顺序就是 b 外 a 内；VHDL 侧若用不同顺序，同一个 `Idx_v` 在两侧指向不同输入，对拍会假错。

**练习 2**：如果有人把双层循环改成 `for a ... for b ...`（a 外 b 内），会发生什么？
**答**：`Expected_c(Idx_v)` 的下标与实际 `(a,b)` 错位，绝大多数用例会立刻报「Error at index …」，且报错的实数值与格式看上去毫无规律——这是典型的「输入重生成顺序不一致」症状。

---

### 4.3 cl_fix_round_tb：RTL 例化、握手与 meta 旁路

#### 4.3.1 概念说明

`cl_fix_round_tb` 验证的是 **RTL 实体** `en_cl_fix_round`（u7-l1 讲过的可综合组件），而不是库函数。因此它必须例化 DUT、跑时钟、走 `in_valid`/`out_valid` 握手，还要验证 `meta` 旁路通道（与数据同拍前进、不参与运算的元数据，u7-l1/u7-l2）。

它的结构比 `cl_fix_add_tb` 复杂一档，但骨架仍是「读格式 → 重生成输入 → 调用 → 比对」，只是「调用」换成了「驱动 DUT 端口」。

#### 4.3.2 核心流程

```
主进程：while test_suite → run("test") → 等待 (and finished) = '1'

generate 每个用例 i（并行）：
  ├─ 常量：RandSeed_c（确定性种子）、Amin..Amax
  ├─ p_input：复位 → for a in Amin..Amax：
  │            in_valid=1; in_meta=RandSlv(种子); in_data=from_integer(a)
  │            ↑ 输入用计数器重生成，meta 用固定种子随机
  ├─ i_uut : entity work.en_cl_fix_round   ← 待测 RTL，reg_mode 随 i 轮换
  └─ p_check：读 Expected；for a in Amin..Amax：
              wait until out_valid & rising_edge(clk)
              比对 out_meta == 同种子重生成的 RandSlv   ← meta 旁路验证
              比对 out_data == Expected(Idx)
              finished(i) <= '1'
```

两个精妙之处：

1. **meta 用固定种子的伪随机**。`p_input` 和 `p_check` 各自 `InitSeed(RandSeed_c)` 后调用 `RandSlv`，因为种子相同、调用次数相同，两侧生成的随机序列**完全一致**。于是「meta 是否原样穿透 DUT」就能被验证——DUT 不该动 meta，所以收到的 meta 必须等于发送侧用同种子重算的值。
2. **`reg_mode` 随用例轮换**。`reg_mode_g => RegisterMode_t'val(i mod reg_mode_count_c)` 让不同用例分别走 Auto/Yes/No 三档寄存器模式，于是同一份黄金参考同时覆盖了「0/1 拍延迟」三种配置（u7-l2），无需写三个 testbench。

#### 4.3.3 源码精读

**配置与完成信号**：

[tb/cl_fix_round_tb.vhd:L56-L70](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd#L56-L70) —— 注意两点：一是 `reg_mode_count_c := 1 + RegisterMode_t'pos(RegisterMode_t'high)`，这是求「枚举值个数」的惯用法（注释指出 VHDL-2019 才有离散类型的 `'length`）；二是 `finished` 是一个位向量，每个用例完成后把自己那一位置 1。

**主进程只做「等」**：

[tb/cl_fix_round_tb.vhd:L87-L99](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd#L87-L99) —— `run("test")` 分支里只有一句 `wait until (and finished) = '1'`（`and` 是对位向量的归约与）。真正的活儿都在 `generate` 出来的并行块里。

**输入进程（重生成输入 + 随机 meta）**：

[tb/cl_fix_round_tb.vhd:L122-L147](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd#L122-L147) —— 复位一拍后，`for a in Amin to Amax` 用计数器逐个喂入 `in_data`，每拍同时喂一个 `RandSlv(meta_width_g)` 作为 meta；计数器走完置 `in_valid='0'` 并把数据线置 `X`（不关心），然后 `wait` 永久挂起。

**DUT 例化**：

[tb/cl_fix_round_tb.vhd:L152-L172](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd#L152-L172) —— 例化 `en_cl_fix_round`，`round_g` 同样用 `FixRound_t'val(rnd_c(i))` 还原枚举，`reg_mode_g` 随 `i` 轮换。

**检查进程（数据 + meta 双重比对）**：

[tb/cl_fix_round_tb.vhd:L177-L203](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd#L177-L203) —— 先用 `cl_fix_read_file` 读期望（注意此处引用写作 `DataPath_c`，与声明的 `datapath_c` 是同一标识符，VHDL 大小写不敏感）；随后 `for a in Amin to Amax`：每个有效输出拍上，先用**同种子**重算 `RandSlv` 与 `out_meta` 比对（验证 meta 旁路），再比 `out_data` 与 `Expected_c(Idx_v)`（验证数据），最后 `finished(i) <= '1'`。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：阅读 `cl_fix_round_tb.vhd`，画出「读格式 → 重生成输入 → round → 比对」的完整流程，并标注所有文件 I/O 调用点。

**操作步骤**：

1. 打开 [tb/cl_fix_round_tb.vhd](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd)。
2. 用三种颜色/标记分别标出：
   - **读格式**：[L59-L60](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd#L59-L60) 的 `cl_fix_read_format_file`（`a_fmt.txt`、`r_fmt.txt`）与 [L63](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd#L63) 的 `read_file`（`rnd.txt`）。
   - **重生成输入**：[L135-L140](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd#L135-L140) 的 `for a in Amin to Amax` 计数器 + `cl_fix_from_integer`。
   - **调用（round）**：DUT 例化 [L152-L159](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd#L152-L159)，`round_g => FixRound_t'val(rnd_c(i))`。
   - **比对**：[L178](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd#L178) 读期望、[L188](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd#L188) 比对 meta、[L191-L197](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/cl_fix_round_tb.vhd#L191-L197) 比对数据。
3. 把上述节点串成一张流程图（纸笔即可）。

**需要观察的现象**：输入侧每拍喂一个计数器值 + 一个随机 meta；输出侧每拍先核对 meta、再核对数据；所有用例完成后 `finished` 全 1，主进程打印 `SUCCESS! All tests passed.`。

**预期结果**：你能指着图说出「文件 I/O 只发生在用例开始时读配置/期望，运行中不再读盘；输入是实时重生成的」。

> 若想真正跑起来（可选）：按 u8-l2 的方式执行 `python sim/run.py --simulator=ghdl`（须先 `pip install -r requirements.txt` 并安装 GHDL/NVC）。能否成功取决于本地仿真器与 VUnit 环境，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`p_input` 和 `p_check` 都调了 `Random_v.InitSeed(RandSeed_c)`，为什么？
**答**：为了用同一颗种子在两侧生成完全相同的伪随机序列。这样 `p_check` 重算的 `RandSlv` 就等于 `p_input` 当初发送的 meta，从而验证 DUT 是否「原样穿透 meta、未做任何改动」。

**练习 2**：主进程的 `wait until (and finished) = '1'` 在等什么？
**答**：等所有用例（`generate` 出来的并行块）都把自己的 `finished(i)` 置 1，即所有用例的数据与 meta 比对全部通过。`(and finished)` 是对位向量的归约与，全 1 时整体为 1。

---

### 4.4 en_cl_fix_fileio_pkg：en_tb 的读写包装与符号转换

#### 4.4.1 概念说明

en_cl_fix 的 testbench 并不直接调 en_tb 的原始读写，而是经过一层薄包装 `en_cl_fix_fileio_pkg`。这层包装解决一个核心矛盾：

- **en_tb 的通用读写**对 `std_logic_vector` 这类「非数值类型」要求**显式声明符号性**（`Signedness_t`，即 `Unsigned_s` / `Signed_s`），因为它无法从一个裸比特向量推断这是有符号还是无符号。
- **en_cl_fix 的 `FixFormat_t`** 已经把符号性编码在 `S` 字段里（S=0 无符号、S=1 有符号，u2-l1/u2-l3）。

包装层做的事就是把 `FixFormat_t.S` 翻译成 `Signedness_t`，再转交给 en_tb。于是 testbench 只要说「这个数据的格式是 `(1,4,8)`」，包装层自动判断它是有符号、该按补码解析文本。

此外，`FixFormat_t` 在文件里存成 `(S,I,F)` 字符串（如 `(1,4,8)`），包装层也提供 `cl_fix_read_format_file` 把整列格式字符串读回 `FixFormatArray_t`。

#### 4.4.2 核心流程

```
┌─────────────── 定点数据读写（带符号转换）────────────────┐
│  cl_fix_read_file(filename, Fmt)                          │
│     → read_file(filename, width(Fmt), to_signedness(Fmt)) │
│        返回 SlvArray_t                                     │
│  cl_fix_read / cl_fix_write：同理，注入 signedness         │
├─────────────── FixFormat_t 读写（字符串解析）──────────────┤
│  cl_fix_read_format_file(filename)                        │
│     → 逐行 cl_fix_format_from_string("(S,I,F)")           │
│        返回 FixFormatArray_t                               │
│  cl_fix_write_format：to_string(Fmt) → "(S,I,F)"          │
└────────────────────────────────────────────────────────────┘
```

关键桥梁函数 `cl_fix_to_signedness`：

\[ \text{signedness}(fmt) = \begin{cases} \text{Signed\_s} & fmt.S = 1 \\ \text{Unsigned\_s} & fmt.S = 0 \end{cases} \]

#### 4.4.3 源码精读

**符号转换桥梁**：

[tb/util/en_cl_fix_fileio_pkg.vhd:L236-L243](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/util/en_cl_fix_fileio_pkg.vhd#L236-L243) —— 整个包装层的「魂」：从 `Fmt.S` 推出 `Signedness_t`。

**定点数据读（注入符号性）**：

[tb/util/en_cl_fix_fileio_pkg.vhd:L254-L262](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/util/en_cl_fix_fileio_pkg.vhd#L254-L262) —— `cl_fix_read`（行级）直接转发给 en_tb 的 `read(L, Data, signedness, TextMode)`。

[tb/util/en_cl_fix_fileio_pkg.vhd:L286-L294](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/util/en_cl_fix_fileio_pkg.vhd#L286-L294) —— `cl_fix_read_file`（整文件）转发给 en_tb 的 `read_file`，把 `cl_fix_width(Fmt)` 当位宽、`cl_fix_to_signedness(Fmt)` 当符号性，返回 `SlvArray_t`（即 `std_logic_vector` 数组，类型定义在 [lib/en_tb/hdl/en_tb_base_pkg.vhd:L62](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/lib/en_tb/hdl/en_tb_base_pkg.vhd#L62)）。对应的 en_tb 重载是 [lib/en_tb/hdl/en_tb_fileio_text_pkg.vhd:L394-L401](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/lib/en_tb/hdl/en_tb_fileio_text_pkg.vhd#L394-L401)（带 signedness、返回 `SlvArray_t` 的那个）。

**FixFormat_t 读（字符串解析）**：

[tb/util/en_cl_fix_fileio_pkg.vhd:L301-L308](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/util/en_cl_fix_fileio_pkg.vhd#L301-L308) —— `cl_fix_read_format`（行级）调用本库的 `cl_fix_format_from_string(L.all)`（来自 `en_cl_fix_pkg`），把 `"(1,4,8)"` 这样的字符串解析回 `FixFormat_t`，然后清空该行。

[tb/util/en_cl_fix_fileio_pkg.vhd:L330-L349](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/util/en_cl_fix_fileio_pkg.vhd#L330-L349) —— `cl_fix_read_format_file`（整文件）先用 `get_file_size_lines` 数行数、`skip_lines` 跳过表头（默认跳 1 行），再逐行解析。

**写侧完全镜像**（此处不逐条展开）：`cl_fix_write` 注入符号性后转发 en_tb `write`；`cl_fix_write_format` 用 `to_string(Fmt)` 把格式写成 `"(S,I,F)"`。

#### 4.4.4 代码实践

**实践目标**：验证「符号性由 `S` 决定」这条规则在解析层的体现。

**操作步骤**：

1. 读 [tb/util/en_cl_fix_fileio_pkg.vhd:L236-L243](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/tb/util/en_cl_fix_fileio_pkg.vhd#L236-L243) 的 `cl_fix_to_signedness`。
2. 读字符串解析器 [hdl/en_cl_fix_pkg.vhd:L729-L758](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L729-L758) 的 `cl_fix_format_from_string`，注意它如何挑出 `(` 后第一个字符当 `S`（只接受 `'0'`/`'1'`，否则 `severity Failure`）。
3. 对照写侧 [hdl/en_cl_fix_pkg.vhd:L695-L698](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L695-L698) 的 `to_string(Fmt)`，确认它把 `S` 写在 `(` 后第一位——读写对称。

**预期结果**：你能解释「为什么 en_tb 必须被告知符号性、而 en_cl_fix 不用」——因为 `FixFormat_t` 自带 `S`，包装层代为声明。

#### 4.4.5 小练习与答案

**练习 1**：如果某个 testbench 误把一个有符号格式 `(1,4,8)` 的数据当成 `(0,4,8)`（无符号）去读 `cl_fix_read_file`，会发生什么？
**答**：`cl_fix_to_signedness` 会给出 `Unsigned_s`，en_tb 就不会按补码解释最高位；于是文件里同一个二进制值会被解析成不同的整数，进而对拍失败（典型的「符号性错配」）。

**练习 2**：`cl_fix_read_format` 在解析完之后为什么要把 `L := new string'("")`（清空行）？
**答**：`cl_fix_format_from_string` 接收的是 `L.all`（整行内容），它不会消耗 `line` 的游标；为了让外层的 `readline`/`deallocate` 语义保持干净，包装层手动把行清空，避免残留内容被误用。

---

### 4.5 en_tb 集成、context 与库边界

#### 4.5.1 概念说明

`en_tb` 是 Enclustra 的**通用 testbench 库**（位于 `lib/en_tb/`），独立于 en_cl_fix。它的定位在 README 里说得很明确：**只用于 testbench，绝不用于 RTL**，因为含不可综合代码。它提供文本文件读写（支持 ascii_bin/ascii_dec/ascii_hex 三种表示）、覆盖整数、`std_logic_vector`、`unsigned`、`signed` 等多种类型，并把「符号性」作为显式参数暴露给非数值类型。

en_cl_fix 把它作为依赖：`en_cl_fix_fileio_pkg` 就是建在它之上的定点专用层。

#### 4.5.2 核心流程

en_tb 在项目里的「接入方式」是固定的三步：

```
1. 编译：sim/run.py 把 en_tb 的 .vhd 编进名为 en_tb 的 VHDL 库（见 u8-l2）
2. 引用：library en_tb;  context en_tb.en_tb_fileio_context;
         （README 明确要求用 context，不用 use 子句）
3. 使用：read_file / read / write / SlvArray_t / Signedness_t …
```

`en_tb_fileio_context` 这个 context 把 `en_tb_fileio_text_pkg`（及其依赖的基础类型）一次性引入，testbench 顶部一行 `context` 就能拿到全部文件 I/O 能力。

#### 4.5.3 源码精读

**库定位（不可综合、仅 testbench）**：

[lib/en_tb/README.md:L26-L35](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/lib/en_tb/README.md#L26-L35) —— 「This library should never be used for RTL development since it contains non-synthesizable code」，并给出用 context 引用的标准范例。

**context 文档（它包含什么）**：

[lib/en_tb/doc/en_tb_fileio_context/readme.md:L5-L11](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/lib/en_tb/doc/en_tb_fileio_context/readme.md#L5-L11) —— 说明该 context 提供 ascii 文本读写、支持多种 VHDL 类型。

**底层类型来源**：

[lib/en_tb/hdl/en_tb_base_pkg.vhd:L57-L62](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/lib/en_tb/hdl/en_tb_base_pkg.vhd#L57-L62) —— `Signedness_t is (Unsigned_s, Signed_s)` 与 `type SlvArray_t is array(integer range<>) of std_logic_vector` 都定义在 base 包里，被 fileio 包/context 传递出来。

**整数文件读（rnd/sat 直接用）**：

[lib/en_tb/hdl/en_tb_fileio_text_pkg.vhd:L379-L383](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/lib/en_tb/hdl/en_tb_fileio_text_pkg.vhd#L379-L383) —— `read_file(...) return integer_vector` 这个重载正是 `cl_fix_add_tb` 里读 `rnd.txt`/`sat.txt` 用的（无需包装，因为整数没有符号性歧义）。

**整库目录索引**：

[lib/en_tb/doc/index.md:L1-L10](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/lib/en_tb/doc/index.md#L1-L10) —— 列出 en_tb 的两个公开入口：fileio context 与 base pkg。

#### 4.5.4 代码实践

**实践目标**：摸清「testbench 里用到的每个符号各来自哪一层」。

**操作步骤**：

1. 在 `cl_fix_add_tb.vhd` 里找出这些符号，并标注来源库：
   - `cl_fix_read_format_file` / `cl_fix_read_file`（包装层 `work.en_cl_fix_fileio_pkg`）
   - `read_file`（直接来自 `en_tb`，返回 `integer_vector`）
   - `FixFormatArray_t`（`work.en_cl_fix_pkg`，[hdl/en_cl_fix_pkg.vhd:L47](https://github.com/enclustra/en_cl_fix/blob/d2ce1a617bc8ba466244a4e500a6edbeea6dba7a/hdl/en_cl_fix_pkg.vhd#L47)）
   - `SlvArray_t`（`en_tb`，经 context 引入）
2. 画一张三层依赖图：`cl_fix_*_tb` → `en_cl_fix_fileio_pkg` → `en_tb`。

**预期结果**：你能一眼区分「哪些符号是 en_cl_fix 自己的、哪些是借 en_tb 的」，并理解为什么 `rnd.txt`/`sat.txt` 不走包装层（整数无符号性歧义，en_tb 原生即可）。

#### 4.5.5 小练习与答案

**练习 1**：README 为什么要求「用 `context` 而非 `use` 子句」引用 en_tb？
**答**：context 是 en_tb 设计的对外门面，一次性把一组互相关联的包（fileio text pkg + base pkg 的可见类型）按正确组合引入，避免使用者漏引某个依赖包导致类型不可见；用 use 子句则需要自己逐个罗列，容易出错。

**练习 2**：en_tb 能否被综合进 FPGA 比特流？
**答**：不能。README 明确它含不可综合代码，仅用于 testbench；en_cl_fix 的可综合部分是 `hdl/` 下的 RTL 与 `en_cl_fix_pkg`（VHDL-93），en_tb 与 testbench 都走 VHDL-2008（u1-l1、u8-l2）。

## 5. 综合实践

把本讲的三块知识串起来：**给一个新的 cosim 用例写一个最小 testbench 骨架**。

设定：假设 cosim 已经为 `cl_fix_neg` 生成了 `data/a_fmt.txt`、`data/r_fmt.txt`、`data/test{N}_output.txt`（取反不涉及舍入/饱和，所以没有 rnd/sat）。请你参照本讲两种写法，写一个**函数级** testbench 的伪代码骨架（不必编译，重在结构）。

参考要点（先自己写，再对照）：

```text
library 声明：
    ieee, vunit_lib (vunit_context), en_tb (en_tb_fileio_context),
    work.en_cl_fix_pkg, work.en_cl_fix_fileio_pkg

architecture 声明区：
    DataPath_c := tb_path(runner_cfg) & "../bittrue/cosim/cl_fix_neg/data/"
    a_fmt_c := cl_fix_read_format_file(DataPath_c & "a_fmt.txt")
    r_fmt_c := cl_fix_read_format_file(DataPath_c & "r_fmt.txt")
    test_count_c := a_fmt_c'length
    Clk 信号（仅为防迭代上限）

procedure Check(i)：
    Expected := cl_fix_read_file(DataPath_c & "test"&i&"_output.txt", r_fmt_c(i))
    Amin/Amax := cl_fix_to_integer(cl_fix_min/max_value(a_fmt_c(i)), a_fmt_c(i))
    for a in Amin to Amax:           ← 单输入，单层循环即可
        Result := cl_fix_neg(cl_fix_from_integer(a, a_fmt_c(i)), a_fmt_c(i), r_fmt_c(i))
        比对 Result == Expected(Idx)；Idx += 1
        wait until rising_edge(Clk)

p_main：
    test_runner_setup；while test_suite → run("test") → for i in 0..test_count_c-1 → Check(i)
    print "SUCCESS!"；test_runner_cleanup
```

**检查清单**：

- [ ] 是否用了 `cl_fix_read_format_file`（而非裸 `read_file`）读格式？
- [ ] 输入是否用计数器重生成、而非从文件读？
- [ ] 循环顺序是否与 cosim 侧一致（`cl_fix_neg` 单输入，无对齐负担，但要确认 cosim 也是单层遍历）？
- [ ] 是否引用了 en_tb 的 context？

> 若你愿意把它真正实现并跑起来，可仿照 `sim/run.py` 里其它 TB 的注册方式（u8-l2），但要确认 cosim 真的生成了对应文件——**待本地验证**。

## 6. 本讲小结

- en_cl_fix 有两种 testbench：**函数级对拍**（`cl_fix_add_tb`，直接调库函数，验证算法）与 **RTL 例化对拍**（`cl_fix_round_tb`，例化实体，验证硬件封装含握手/meta）。
- 两者共享四步骨架：**读配置 → 重生成输入 → 调用待测对象 → 与文件期望逐位比对**。
- 核心取舍是「**只存输出、不存输入**」：输入靠 Python 与 VHDL 两侧用同一计数规则（b 外 a 内）各自重生成，顺序必须严格对齐，否则下标错位假错。
- 枚举模式（round/sat）经「`.value` 写盘 → `'val` 读回」的位置码往返传递，两侧枚举顺序不可打乱。
- `en_cl_fix_fileio_pkg` 是对 en_tb 的薄包装，核心是 `cl_fix_to_signedness`：把 `FixFormat_t.S` 翻译成 en_tb 要求的 `Signedness_t`；格式串 `(S,I,F)` 由 `cl_fix_format_from_string` / `to_string` 读写对称处理。
- `en_tb` 是通用、不可综合的 testbench 库，通过 `context en_tb.en_tb_fileio_context` 引入；普通整数文件（rnd/sat）直接用其原生 `read_file`，定点数据与格式才走包装层。

## 7. 下一步学习建议

- **向上回看**：结合 u8-l1、u8-l2，你已经掌握 cosim 闭环的全部三段（Python 黄金参考 → VUnit 调度 → VHDL 对拍）。建议回头重读一个完整 cosim 目录（如 `bittrue/cosim/cl_fix_add/cosim.py`），对照本讲的 TB，确认两侧的格式/模式枚举与遍历顺序完全对齐。
- **向 RTL 深入**：若对 `cl_fix_round_tb` 例化的 `en_cl_fix_round` 实体本身感兴趣，进入单元 7（u7-l1 可综合 RTL 组件、u7-l2 流水线与 RegisterMode）。
- **测试体系**：若想看 Python 侧如何独立验证算法（不依赖 VHDL），进入 u9-l2（Python 单元测试体系与边界用例）。
- **动手扩展**：尝试按综合实践，为 `cl_fix_neg` 或 `cl_fix_abs` 写一个 testbench 骨架，并对照仓库里已有的 `cl_fix_neg_tb.vhd` / `cl_fix_abs_tb.vhd` 自查。

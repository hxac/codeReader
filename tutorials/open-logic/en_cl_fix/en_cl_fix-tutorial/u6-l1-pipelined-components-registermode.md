# round / saturate / resize 组件与 RegisterMode

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `hdl/` 下三个可实例化组件 `en_cl_fix_round` / `en_cl_fix_saturate` / `en_cl_fix_resize` 与 u5 讲过的纯组合函数 `cl_fix_round` / `cl_fix_saturate` / `cl_fix_resize` 之间的封装关系。
- 看懂三个组件统一的 `clk / rst / in_valid / in_meta / in_data / out_valid / out_meta / out_data` 端口约定，以及它们各自的 `generics`。
- 解释 `RegisterMode_t` 的 `Auto_s / Yes_s / No_s` 三态分别对应什么样的延迟与寄存器插入策略。
- 理解 `cl_fix_recommended_pipelining` 三个重载如何「预测」一组配置到底需不需要寄存器，从而让 `Auto_s` 能自动选出 0 拍或 1 拍延迟。
- 读懂 `use_reg_c` 这个编译期布尔常量如何通过 `generate` 在「时钟进程」与「连续赋值」两条分支之间二选一。

## 2. 前置知识

本讲承接 u5-l2 与 u5-l3。在 u5 里我们已经知道：

- `cl_fix_round` / `cl_fix_saturate` / `cl_fix_resize` 是 `en_cl_fix_pkg` 里的**纯组合函数**，输入一个 `std_logic_vector` 和格式参数，立即输出结果，**不包含任何时钟、不占任何时钟周期**（0 拍延迟）。
- `cl_fix_resize = 先 round 后 saturate`，顺序不可交换。
- 这些函数是真正的「数学」，可以被任何调用方直接使用。

但在真实的 FPGA 数据通路里，把一长串组合逻辑（乘法、舍入、饱和……）首尾相连，会在两个寄存器之间形成一条很长的组合路径，导致时序无法收敛（时钟跑不高）。解决办法很简单：**在关键算子的输出端插一级寄存器**，把长路径切断。

问题是：插寄存器意味着多 1 拍延迟。有些算子本身就是「零逻辑」（比如纯截断 `Trunc_s`，连一个加法器都不需要），给它强行插寄存器纯属浪费延迟。于是 en_cl_fix 给了一套机制，让综合器**按需**插寄存器——这就是本讲的主角：三个**可流水线化（pipelined）组件**和 `RegisterMode_t`。

> 术语提示：本讲的「组件」指 `entity ... end entity` 这种可被 `entity work.xxx` 例化的 RTL 模块；「纯函数」指 `en_cl_fix_pkg` 里的 `function`。组件在内部调用纯函数完成数值计算，再决定要不要套一层寄存器。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [hdl/en_cl_fix_round.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd) | 可流水线化的舍入组件 | 实体 generics、`use_reg_c`、`g_register`/`g_no_register` 双分支 |
| [hdl/en_cl_fix_saturate.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_saturate.vhd) | 可流水线化的饱和组件 | 与 round 组件几乎逐字一致的结构 |
| [hdl/en_cl_fix_resize.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd) | 可流水线化的 resize 组件（round + saturate） | 不自带寄存器分支，而是**级联例化**前两个组件 |
| [hdl/en_cl_fix_pkg.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd) | VHDL 包 | `RegisterMode_t` 定义、`cl_fix_recommended_pipelining` 三个重载、被组件调用的纯函数 |
| [tb/cl_fix_round_tb.vhd](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd) | round 组件的测试台 | 真实例化方式、用 `RegisterMode_t'val` 轮换三种寄存器模式 |

## 4. 核心概念与源码讲解

### 4.1 从纯函数到可流水线组件：实体、generics 与统一端口

#### 4.1.1 概念说明

u5 讲的 `cl_fix_round` 是一个函数，没有时钟。本讲的 `en_cl_fix_round` 是一个 **entity**，有 `clk` 和 `rst`，内部第一行就调用同名函数算出结果，然后决定要不要把它锁进寄存器。可以这样理解二者关系：

```
纯函数 cl_fix_round(...)        →  立即出结果，0 拍，组合逻辑
组件  en_cl_fix_round(...)      →  内部调用上面的函数，可选地套 1 拍寄存器
```

三个组件 `en_cl_fix_round` / `en_cl_fix_saturate` / `en_cl_fix_resize` 的设计哲学完全一致：**把 u5 的纯函数包一层，外加一个统一的「可选寄存器」外壳**。其中 `en_cl_fix_resize` 还更进一步——它内部不是再写一遍 round+saturate，而是直接**例化**前两个组件，天然复用了它们的寄存器机制。

#### 4.1.2 核心流程

三个组件对外的接口约定是统一的，可以画成一条「带 valid/meta 旁路的数据流水段」：

```
          ┌─────────────────────────────────────┐
clk,rst → │                                     │
in_valid→ │  ┌──────────┐    ┌───────────────┐  │ → out_valid
in_meta → │  │ 纯函数计算 │ → │ (可选) 寄存器  │  │ → out_meta
in_data → │  │ result   │    │  clk 锁存      │  │ → out_data
          │  └──────────┘    └───────────────┘  │
          └─────────────────────────────────────┘
```

- `in_valid` / `out_valid`：数据有效标志，与数据同步流过（寄存器模式下延迟 1 拍）。
- `in_meta` / `out_meta`：边带元数据（sideband metadata），例如「这一拍属于哪个通道」「这是帧的第几个采样」等。它**不参与数值计算**，只跟随数据一起被寄存器锁存，保证数据与 meta 在时间上对齐。
- `in_data` / `out_data`：定点数据本身，位宽由 `in_fmt_g` / `out_fmt_g` 经 `cl_fix_width` 计算得到。

#### 4.1.3 源码精读

先看 round 组件的实体 generics，它有 5 个参数：[hdl/en_cl_fix_round.vhd:L50-L58](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L50-L58) 定义了 `in_fmt_g / out_fmt_g / round_g / reg_mode_g / meta_width_g / fmt_check_g`。其中：

- `in_fmt_g` / `out_fmt_g`：输入输出格式，直接传给内部纯函数。
- `round_g`：舍入模式（u2-l2 的七种之一）。
- `reg_mode_g`：寄存器策略（本讲主角，4.2 详解）。
- `meta_width_g`：边带位宽，缺省 `0`（即不用 meta）。
- `fmt_check_g`：是否在仿真期检查 `out_fmt_g` 是否等于 `cl_fix_round_fmt(...)` 的合法预测值，缺省 `true`。

端口定义在 [hdl/en_cl_fix_round.vhd:L59-L77](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L59-L77)，注意两个细节：

1. `in_data` 的位宽是 `cl_fix_width(in_fmt_g)-1 downto 0`，`out_data` 是 `cl_fix_width(out_fmt_g)-1 downto 0`——位宽在**综合期**就由格式参数钉死。
2. `in_meta` 用了 `:= (others => 'X')` 作为默认值。当 `meta_width_g = 0` 时，这个 `std_logic_vector(-1 downto 0)` 是空范围，`(others => 'X')` 让它合法存在而不报错。

saturate 组件的实体几乎一模一样：[hdl/en_cl_fix_saturate.vhd:L50-L77](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_saturate.vhd#L50-L77)。唯一差别是它用 `saturate_g : FixSaturate_t` 取代了 `round_g`，并且**没有** `fmt_check_g`（饱和不改变小数位，无需该项检查）。

resize 组件最特殊：[hdl/en_cl_fix_resize.vhd:L50-L58](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd#L50-L58) 同时带 `round_g` 和 `saturate_g`（因为 resize = 先 round 后 saturate），端口约定与前两者相同。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用一张表把三个组件的 generics 对齐，确认它们的「同构性」。

**步骤**：

1. 打开三个 `.vhd` 的 entity 段。
2. 按下表逐格填写每个组件是否有该 generic：

   | generic | en_cl_fix_round | en_cl_fix_saturate | en_cl_fix_resize |
   | --- | --- | --- | --- |
   | `in_fmt_g` | ? | ? | ? |
   | `out_fmt_g` | ? | ? | ? |
   | `round_g` | ? | ? | ? |
   | `saturate_g` | ? | ? | ? |
   | `reg_mode_g` | ? | ? | ? |
   | `meta_width_g` | ? | ? | ? |
   | `fmt_check_g` | ? | ? | ? |

**预期结果**：前 5 项三者全有；`round_g` 仅 round/resize 有；`saturate_g` 仅 saturate/resize 有；`fmt_check_g` **只有 round 有**。这张表正好解释了为什么 resize 是「round + saturate 的并集」。

#### 4.1.5 小练习与答案

**练习**：`in_meta` 端口的默认值为什么写成 `:= (others => 'X')` 而不是 `:= (others => '0')`？当 `meta_width_g = 0` 时它到底是什么？

**参考答案**：`'X'`（未知态）表示「调用方若不接这根线，就是没定义」，仿真里更易暴露「忘了接 meta」的错误；用 `'0'` 会掩盖问题。当 `meta_width_g = 0` 时，范围是 `-1 downto 0`，是一个**空数组**（null range），无论填什么都合法、且不占任何比特。

---

### 4.2 RegisterMode_t：三态寄存器策略与延迟语义

#### 4.2.1 概念说明

`reg_mode_g` 的类型是 `RegisterMode_t`，这是 u5-l1 提到过的三个枚举之一。它**只控制流水线组件是否插寄存器**，不参与任何数值计算。三个值的语义是：

- `Auto_s`：**按需**插寄存器。需要时插 1 级，不需要时插 0 级。延迟随其他 generics 变化。
- `Yes_s`：**强制**插寄存器。无论是否需要，都插。延迟恒定，便于跨配置保持一致的流水线深度。
- `No_s`：**从不**插寄存器。纯组合，0 拍。注释明确警告「通常会让时序变差，慎用」。

这三个值回答的是一个工程权衡问题：你更在意「最小延迟」还是「恒定延迟」还是「最快编译」。

#### 4.2.2 核心流程

把延迟（latency，单位为时钟周期）列成表：

| `reg_mode_g` | round 组件延迟 | saturate 组件延迟 | resize 组件延迟 |
| --- | --- | --- | --- |
| `No_s` | 0 | 0 | 0 |
| `Auto_s` | `cl_fix_recommended_pipelining(...)` ∈ {0, 1} | ∈ {0, 1} | ∈ {0, 1, 2} |
| `Yes_s` | 1 | 1 | 2 |

注意 resize 的 `Yes_s` 是 **2**，而不是 1。因为 resize 内部级联了 round 与 saturate 两个子组件（4.4 详解），每个最多贡献 1 拍，`Yes_s` 下两者都插寄存器，共 2 拍。这正是 resize 组件头注释里写的 `Latency = 2`：[hdl/en_cl_fix_resize.vhd:L31-L35](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd#L31-L35)。

定义见 [hdl/en_cl_fix_pkg.vhd:L68-L73](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L68-L73)：

```vhdl
type RegisterMode_t is
(
    Auto_s,         -- Inserts the recommended registering. See cl_fix_recommended_pipelining.
    Yes_s,          -- Inserts all registering. Can be useful for consistent latency.
    No_s            -- Inserts no registering. Use with caution (poor timing performance).
);
```

#### 4.2.3 源码精读

三个组件的文件头注释用一致的三段式说明了这三种模式的延迟，例如 round 组件：[hdl/en_cl_fix_round.vhd:L27-L35](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L27-L35)。注释里有一句关键提示：

> If unsure, set to `Yes_s`.

也就是「不知道选哪个就用 `Yes_s`」。原因是 `Yes_s` 延迟恒定、一定有寄存器切断路径，是最安全的默认；`Auto_s` 虽然延迟更优，但延迟会随格式变化，需要调用方自己清楚这一点。

`RegisterMode_t` 只在这三个流水线组件里被消费，纯函数（`cl_fix_round` 等）完全不认识它——这印证了 u5-l1 的结论：`RegisterMode_t` 是「组件层」的概念，与「数学层」无关。

#### 4.2.4 代码实践（源码阅读型）

**目标**：从源码注释中提取三种模式的确切延迟承诺。

**步骤**：

1. 分别打开三个组件文件，阅读 `-- Description:` 注释块中关于 `reg_mode_g` 的三段说明。
2. 记下每个组件在 `Auto_s` 下引用的 `cl_fix_recommended_pipelining(...)` 签名（参数列表不同）。
3. 对照 4.2.2 的延迟表，确认 `Yes_s` 下 round=1、saturate=1、resize=2。

**预期结果**：三个文件的注释措辞几乎一致，唯独 resize 把 `Latency = 1` 换成了 `Latency = 2`，且 `Auto_s` 行引用的 `cl_fix_recommended_pipelining` 多带了 `round_g, saturate_g` 两个参数。

#### 4.2.5 小练习与答案

**练习 1**：为什么组件注释建议「不确定时用 `Yes_s`」而不是 `Auto_s`？

**参考答案**：`Yes_s` 保证延迟恒定且必有寄存器，时序和功能行为最可预测；`Auto_s` 延迟会随 `in_fmt_g/out_fmt_g/round_g` 变化，若调用方没意识到这一点，可能在级联多个组件时出现数据与 meta 错拍。

**练习 2**：`No_s` 在什么场景下才合理？

**参考答案**：当该组件前后已经紧邻其他寄存器（例如前面是一个 DSP 块的输出寄存器，后面立刻进另一级寄存器），中间这级组合逻辑很短、不会再恶化时序，此时 `No_s` 可省一拍延迟。属于「确知路径很短」的精细优化，故名注释警告慎用。

---

### 4.3 cl_fix_recommended_pipelining：三个重载如何预测「需不需要寄存器」

#### 4.3.1 概念说明

`Auto_s` 之所以能「按需」插寄存器，靠的是一个纯函数 `cl_fix_recommended_pipelining`。它回答一个问题：**给定一组格式与模式参数，这一级算子到底要不要寄存器？** 返回值是「推荐的流水线级数」，对 round/saturate 是 0 或 1，对 resize 是 0/1/2。

它的判定原则非常朴素——**「零逻辑」就不需要寄存器**。所谓零逻辑，是指这级算子实际上不产生任何有意义的组合电路（或电路简单到可以忽略）。比如纯截断 `Trunc_s` 连一个加法器都没有，只是丢低位，给它套寄存器纯属浪费。

为了适配 round / saturate / resize 三种算子，这个函数名有三个**重载**（VHDL 允许同名函数按参数列表区分），声明见 [hdl/en_cl_fix_pkg.vhd:L164-L185](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L164-L185)。

#### 4.3.2 核心流程

三个重载各自的「零逻辑」判定条件：

**round 重载**（参数 `a_fmt, result_fmt, round`）返回 0 的两种情况：

1. `round = Trunc_s`——截断不产生加法器；
2. `result_fmt.F >= a_fmt.F`——没有丢小数位，舍入无从发生。

否则返回 1。用一个表达式概括：

\[
\text{pipelining}_{\text{round}} =
\begin{cases}
0 & \text{若 } \text{round} = \text{Trunc\_s} \text{ 或 } \text{result\_fmt}.F \geq \text{a\_fmt}.F \\
1 & \text{其他}
\end{cases}
\]

**saturate 重载**（参数 `a_fmt, result_fmt, saturate`）返回 0 的两种情况：

1. `saturate ∈ {None_s, Warn_s}`——只回绕不钳位，钳位比较器不存在；
2. `result_fmt.I >= a_fmt.I 且 result_fmt.S = a_fmt.S`——整数位没被压缩、符号位没变，根本不会越界，钳位逻辑虽然存在但永不被触发。

否则返回 1。注意该重载一开始就 `assert result_fmt.F = a_fmt.F`，因为饱和不允许改变小数位（u2-l3 / u5-l2 已述）。

**resize 重载**（参数 `a_fmt, result_fmt, round, saturate`）：不自己判断，而是**先算出舍入后的中间格式 `round_fmt_c`，再把前两个重载的返回值相加**：

\[
\text{pipelining}_{\text{resize}} =
\text{pipelining}_{\text{round}}(\text{a\_fmt} \to \text{round\_fmt}) +
\text{pipelining}_{\text{sat}}(\text{round\_fmt} \to \text{result\_fmt})
\]

这正是 resize 内部「先 round 后 saturate」结构在寄存器层面的镜像。

#### 4.3.3 源码精读

round 重载实现：[hdl/en_cl_fix_pkg.vhd:L1041-L1069](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1041-L1069)。注意它先做 `fmt_check` 断言，要求 `result_fmt` 必须等于 `cl_fix_round_fmt(a_fmt, result_fmt.F, round)`（u3-l3 讲过的格式契约）；随后两个 `if` 分别对应上面两种「零逻辑」情形。

saturate 重载实现：[hdl/en_cl_fix_pkg.vhd:L1071-L1097](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1071-L1097)。开头强制 `result_fmt.F = a_fmt.F`，随后判定回绕模式与「整数位/符号位未变」。

resize 重载实现：[hdl/en_cl_fix_pkg.vhd:L1099-L1109](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1099-L1109)，核心两行：

```vhdl
constant round_fmt_c : FixFormat_t := cl_fix_round_fmt(a_fmt, result_fmt.F, round);
return cl_fix_recommended_pipelining(a_fmt, round_fmt_c, round)
     + cl_fix_recommended_pipelining(round_fmt_c, result_fmt, saturate);
```

这正是 4.3.2 公式的直译。

> 一个微妙点：round 组件在计算 `recommended_c` 时**把 `fmt_check_g` 一并传入**（见 [hdl/en_cl_fix_round.vhd:L85](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L85)），saturate/resize 组件则没有对应的 `fmt_check`。所以 round 重载是唯一带 `fmt_check : boolean := true` 参数的重载。

#### 4.3.4 代码实践（手算型）

**目标**：手算几组配置的推荐流水线级数，再与函数语义对照。

**步骤**：对下列每组 `(a_fmt, result_fmt, 模式)`，先用 4.3.2 的规则手算 `cl_fix_recommended_pipelining` 的返回值，然后说明理由。

1. round：`a_fmt = [0,4,4]`，`result_fmt = [0,4,4]`，`round = NonSymPos_s`
2. round：`a_fmt = [0,4,4]`，`result_fmt = [0,4,2]`，`round = Trunc_s`
3. round：`a_fmt = [0,4,4]`，`result_fmt = [0,4,2]`，`round = NonSymPos_s`
4. saturate：`a_fmt = [1,8,0]`，`result_fmt = [1,4,0]`，`saturate = Sat_s`
5. saturate：`a_fmt = [1,8,0]`，`result_fmt = [1,8,0]`，`saturate = Sat_s`

**预期结果**：

1. `0`（`result_fmt.F(4) >= a_fmt.F(4)`，没丢小数位）
2. `0`（`Trunc_s`）
3. `1`（非 Trunc 且丢了 2 位小数）
4. `1`（Sat 模式且整数位从 8 压到 4）
5. `0`（整数位未压缩、符号位未变，即使 Sat 也永不触发）

> 待本地验证：若已装好 VUnit/GHDL，可写一个最小 testbench 打印这些函数返回值确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 saturate 重载里即使 `saturate = Sat_s`，只要「整数位和符号位都没变」就返回 0？

**参考答案**：因为不压缩整数位、不改符号位时，输入值永远落在输出格式范围内，钳位比较器虽然综合出来但**恒为假**，会被综合工具优化掉，等价于零逻辑，故无需寄存器。

**练习 2**：resize 重载返回值的上限为什么是 2 而不是更高？

**参考答案**：它等于 round 部分与 saturate 部分之和，而这两部分各自上限为 1，所以 resize 上限为 2。这正好对应 `Yes_s` 下 resize 的 2 拍延迟。

---

### 4.4 use_reg_c 与 generate 双分支：自动选 0 拍 / 1 拍延迟

#### 4.4.1 概念说明

知道了「推荐级数」之后，组件要把它翻译成实际的硬件。做法极简：在 architecture 里定义一个**编译期布尔常量** `use_reg_c`，再用 `generate` 语句在两条互斥的硬件描述里二选一。因为 `use_reg_c` 依赖的 `reg_mode_g`、`recommended_c` 在综合期都是常量，所以综合器只会留下其中一条分支，另一条被完全丢弃——没有任何面积浪费。

#### 4.4.2 核心流程

`use_reg_c` 的定义（round 组件，saturate 同理）：

```
use_reg_c = (reg_mode_g = Yes_s)
         or (reg_mode_g = Auto_s and recommended_c > 0)
```

- `Yes_s` → 恒为真，强制寄存器。
- `Auto_s` → 仅当推荐级数 > 0 才插。
- `No_s` → 两个条件都不满足，恒为假，纯组合。

两条分支：

- `g_register`（`use_reg_c` 为真）：一个 `process(clk)`，在 `rising_edge(clk)` 把 `result / in_meta / in_valid` 锁进寄存器。`out_valid <= in_valid and not rst` 实现「复位时拉低有效」。
- `g_no_register`（`use_reg_c` 为假）：三句连续赋值 `out_data <= result`、`out_meta <= in_meta`、`out_valid <= in_valid`，0 拍直通。

#### 4.4.3 源码精读

round 组件的常量定义与双分支：[hdl/en_cl_fix_round.vhd:L85-L111](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L85-L111)。关键三行：

```vhdl
constant recommended_c : natural range 0 to 1 := cl_fix_recommended_pipelining(in_fmt_g, out_fmt_g, round_g, fmt_check_g);
constant use_reg_c     : boolean := (reg_mode_g = Yes_s) or (reg_mode_g = Auto_s and recommended_c > 0);
signal result          : std_logic_vector(cl_fix_width(out_fmt_g)-1 downto 0);
```

注意 `recommended_c` 的子类型是 `natural range 0 to 1`——编译器在类型层面就保证 round/saturate 的推荐级数只能是 0 或 1。随后 [hdl/en_cl_fix_round.vhd:L95-L104](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L95-L104) 是寄存器进程，[hdl/en_cl_fix_round.vhd:L107-L111](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L107-L111) 是组合直通。

saturate 组件的对应代码完全同构：[hdl/en_cl_fix_saturate.vhd:L84-L110](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_saturate.vhd#L84-L110)，差别仅在 `recommended_c` 调用的是 saturate 重载、且 `result <= cl_fix_saturate(...)`。

**resize 组件不重复这套模式**，而是用级联例化代替：[hdl/en_cl_fix_resize.vhd:L91-L141](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd#L91-L141)。它先算出中间格式 `round_fmt_c`（[hdl/en_cl_fix_resize.vhd:L85](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd#L85)），然后例化 `i_round`（round 到 `round_fmt_c`）和 `i_saturate`（`round_fmt_c` 到 `out_fmt_g`），把前者的输出直连后者的输入。`reg_mode_g` 被原样透传给两个子组件，于是寄存器决策被**下放**给它们各自用 `use_reg_c` 判断。这就是 resize `Yes_s` 延迟为 2 的根本原因：两个子组件各自插 1 拍。

#### 4.4.4 代码实践（跟踪型，对应本讲核心任务）

**目标**：跟踪「`reg_mode_g = Auto_s` 且舍入实际无需寄存器」时，组件如何自动得到 0 拍延迟。

**步骤**：

1. 取一个零逻辑场景：`in_fmt_g = [0,4,4]`、`out_fmt_g = [0,4,2]`、`round_g = Trunc_s`、`reg_mode_g = Auto_s`。
2. 在 [hdl/en_cl_fix_pkg.vhd:L1041-L1069](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1041-L1069) 求 `recommended_c`：因为 `round = Trunc_s`，函数在第 1056 行直接 `return 0`。
3. 代入 [hdl/en_cl_fix_round.vhd:L86](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L86) 的 `use_reg_c`：`reg_mode_g = Yes_s` 为假；`reg_mode_g = Auto_s` 为真但 `recommended_c > 0` 为假（`0 > 0` 假）；故 `use_reg_c = false`。
4. 于是 `g_register` 分支被丢弃，只有 [hdl/en_cl_fix_round.vhd:L107-L111](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L107-L111) 的 `g_no_register` 生效，`out_data <= result` 直通，**0 拍延迟**。
5. 对比：若把 `round_g` 改成 `NonSymPos_s`（其余不变），步骤 2 的函数走到 `result_fmt.F(2) >= a_fmt.F(4)` 为假，返回 1；步骤 3 `use_reg_c` 变真；组件变为 1 拍延迟。

**需要观察的现象**：同一份组件代码、同一个 `reg_mode_g = Auto_s`，只因 `round_g` 不同就产生 0 拍或 1 拍两种硬件——这正是「Auto 按需」的体现，且决策完全发生在综合期。

**预期结果**：`Trunc_s` → 0 拍；`NonSymPos_s` → 1 拍。无需改任何 RTL，仅靠 generics 切换。

#### 4.4.5 小练习与答案

**练习 1**：`use_reg_c` 为什么必须用 `generate if` 而不是运行期 `if`？

**参考答案**：因为 `use_reg_c` 依赖的全是综合期常量（generics 与由它们算出的 `recommended_c`），`generate if` 让综合器只编译命中的一条分支，省掉另一条的硬件；运行期 `if` 则会同时综合两条分支并用选择器切换，既浪费面积又引入多路器延迟。

**练习 2**：resize 组件为什么没有自己的 `use_reg_c`？

**参考答案**：resize 通过级联例化 round + saturate 子组件实现，寄存器决策被下放给两个子组件各自的 `use_reg_c`。resize 自身只负责把它们的数据/valid/meta 口对接起来，所以不需要再判一次。

---

## 5. 综合实践

**任务**：用本讲的三个组件搭一条「乘法结果 → 舍入 → 饱和」的定点流水线草图，并标注每级的格式与寄存器策略。这是一个贯穿本讲全部知识的小设计。

**步骤**：

1. 假设乘法器输出全精度格式 `mid_fmt = cl_fix_mult_fmt(a_fmt, b_fmt)`（u3-l2），例如 `a_fmt = b_fmt = [1,4,4]` 时 `mid_fmt` 整数位会增长。
2. 设计目标输出格式 `out_fmt = [1,8,4]`（更窄的整数位）。
3. 选择 `round_g = NonSymPos_s`、`saturate_g = SatWarn_s`。
4. 在草图上画出：
   - 第一级：用 `en_cl_fix_resize`（或先 `en_cl_fix_round` 再 `en_cl_fix_saturate`）把 `mid_fmt` 收敛到 `out_fmt`。
   - 标注每级的 `in_fmt_g` / `out_fmt_g`，确认 saturate 一级的 `in_fmt_g.F == out_fmt_g.F`（饱和不改小数位的硬约束）。
   - 标注 `meta_width_g`，说明 meta 信号如何贯穿各级、与数据保持同拍。
5. 选择 `reg_mode_g`：
   - 若希望「无论格式怎么变，总延迟恒定」→ 选 `Yes_s`，总延迟 = resize 的 2 拍。
   - 若希望「按需，能省则省」→ 选 `Auto_s`，并用手算（4.3.4）估算实际延迟。
6. 验证用真实例化模板：参考测试台 [tb/cl_fix_round_tb.vhd:L152-L172](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L152-L172) 中 `i_uut` 的写法，把你草图里的 resize 组件按同样的 `generic map / port map` 风格写成 VHDL 片段。

**进阶观察**：测试台在 [tb/cl_fix_round_tb.vhd:L66](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L66) 与 [tb/cl_fix_round_tb.vhd:L157](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L157) 用 `RegisterMode_t'val(i mod reg_mode_count_c)` 让不同测试用例自动轮换 `Auto_s/Yes_s/No_s` 三种模式，且都通过同一份 checker 比对。这说明：**三种寄存器模式下的功能输出完全一致，只有延迟不同**——这也是为什么调用方可以放心地按延迟需求而非功能需求来选模式。

> 待本地验证：若已配置好仿真器，可运行 `python sim/run.py`（u1-l3）触发 `cl_fix_round_tb`，观察它在不同 `reg_mode_g` 下都报告 `SUCCESS! All tests passed.`。

## 6. 本讲小结

- 三个组件 `en_cl_fix_round` / `en_cl_fix_saturate` / `en_cl_fix_resize` 把 u5 的纯组合函数包了一层「可选寄存器」外壳，提供统一的 `clk/rst/valid/meta/data` 流水线接口。
- `RegisterMode_t` 的 `Auto_s`（按需）/ `Yes_s`（恒定 1 拍，resize 为 2 拍）/ `No_s`（0 拍）只控制寄存器插入，不参与数值计算；不确定时官方建议用 `Yes_s`。
- `cl_fix_recommended_pipelining` 用三个重载分别预测 round / saturate / resize 的「推荐级数」，原则是「零逻辑则返回 0」；resize 重载 = round 部分 + saturate 部分，上限 2。
- 编译期常量 `use_reg_c = (Yes_s) or (Auto_s and recommended_c>0)` 通过 `g_register` / `g_no_register` 两个互斥 `generate` 分支二选一，综合后只保留一条，无面积浪费。
- resize 组件不重复寄存器逻辑，而是级联例化 round + saturate 子组件，把寄存器决策下放，因此其 `Yes_s` 延迟天然为 2。
- `Auto_s` 能自动选 0 拍：当 `recommended_c = 0`（如 `Trunc_s` 或不丢小数位）时 `use_reg_c` 为假，走纯组合直通分支。

## 7. 下一步学习建议

- 下一讲 **u6-l2（搭建流水线数据通路：meta 与延迟一致性）** 会把本讲的单个组件扩展成多级级联的完整数据通路，重点讲 `meta_width_g` 旁路透传与用 `Yes_s` 保证跨配置延迟恒定的工程技巧。
- 建议回头对照阅读 u5-l2（`cl_fix_round/saturate/resize` 的纯函数实现）与 u3-l3（`cl_fix_round_fmt` 的格式预测），确认本讲组件里 `recommended_c` 与 `round_fmt_c` 的来源都已在前面讲义中讲透。
- 若想看这些组件如何被批量验证，可预习 u7-l2（VUnit 测试台与文件 I/O），那里的 `cl_fix_round_tb.vhd` 正是本讲引用的测试台，它会用 cosim 黄金数据逐拍比对 UUT 输出。

# round/saturate/resize 组件与 RegisterMode

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `en_cl_fix_round`、`en_cl_fix_saturate`、`en_cl_fix_resize` 三个可实例化组件的**统一接口约定**（`clk/rst/valid/meta/data`）。
- 解释 `RegisterMode_t` 三种取值（`Auto_s / Yes_s / No_s`）各自代表的寄存器意愿与对应延迟。
- 读懂 `cl_fix_recommended_pipelining` 的**三个重载**，理解库如何判断「这个运算到底需不需要一拍寄存器」。
- 跟踪 `use_reg_c` 布尔常量如何驱动 `g_register / g_no_register` 两条 `generate` 分支，在综合期二选一。
- 说明 `resize` 组件如何通过**级联一个 round 子组件和一个 saturate 子组件**实现「先 round 后 saturate」，并把延迟上限抬到 2。

## 2. 前置知识

本讲是 U6（可流水线化组件）的第一篇，承接你已经掌握的两条线索：

1. **纯函数层的 round/saturate/resize**（u5-l2）：你知道 `cl_fix_round` 走「构造 mid_fmt → 加偏移 → 截断」，`cl_fix_saturate` 走「convert 回绕 → 按需 assert 告警 → 按需钳位」，而 `cl_fix_resize` 固定「先 round 后 saturate」且顺序不可交换。这些是**组合逻辑纯函数**，调用即返回，没有时钟。
2. **数学函数的三段式模板**（u5-l3）：综合期用 `cl_fix_*_fmt` 算格式、运行期 `convert` 对齐、最后 `resize` 收敛。

本讲要回答的新问题是：**这些纯函数组合逻辑，怎么变成 FPGA 上可实例化、可流水线、延迟可预期的实体？** 关键在于两点——一是把组合逻辑包一层带 `clk/rst/valid` 的时序外壳，二是让库自己判断「这一拍到底要不要插寄存器」。

> 名词速查
> - **流水线寄存器（pipeline register）**：在组合逻辑输出端打一拍触发器，把长组合路径切成两段，改善时序（timing），代价是多一拍延迟（latency）。
> - **valid/meta 旁路**：数据通路上常带一个 `valid`（数据是否有效）和一段 `meta`（边带信息，如通道号、时间戳），它们必须与数据**同拍**穿过每一级寄存器，否则错位。
> - **generate 二选一**：VHDL 的 `if ... generate` 在 elaboration 期求值，只有条件成立的分支会被综合，等价于编译期的 `if`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `hdl/en_cl_fix_round.vhd` | 可实例化的**舍入组件**：纯函数 `cl_fix_round` + 可选流水线寄存器 + valid/meta 旁路。 |
| `hdl/en_cl_fix_saturate.vhd` | 可实例化的**饱和组件**：结构与 round 组件几乎完全一致，仅替换内部纯函数与 generic。 |
| `hdl/en_cl_fix_resize.vhd` | 可实例化的**resize 组件**：内部级联一个 round 子组件 + 一个 saturate 子组件，不重复实现逻辑。 |
| `hdl/en_cl_fix_pkg.vhd` | 定义 `RegisterMode_t` 枚举、`cl_fix_recommended_pipelining` 的三个重载，以及组件所依赖的 `cl_fix_round / cl_fix_saturate / cl_fix_round_fmt` 纯函数。 |
| `tb/cl_fix_round_tb.vhd` | 组件的仿真验证台，例化 `en_cl_fix_round` 并在不同测试用例间轮换 `reg_mode_g`。 |

三个组件体都非常短（各 110 行左右），是本讲的主角；包里的 `cl_fix_recommended_pipelining` 是它们「智能插寄存器」的大脑。

## 4. 核心概念与源码讲解

### 4.1 组件实体：统一的 generics 与端口接口

#### 4.1.1 概念说明

U5 讲的 `cl_fix_round` 等都是**纯函数**——给输入位串立刻返回输出位串，没有时钟、没有寄存器。但在真实 FPGA 设计里，你很少会直接调纯函数做一长串组合逻辑，因为：

- 一条贯穿「乘法 → 舍入 → 饱和」的纯组合路径可能太长，**时序跑不到目标频率**。
- 你需要标准的 **valid 流接口**来对接前后级流水线。
- 你希望**延迟是确定的、可推导的**，而不是「看综合工具心情」。

于是库提供了三个**可实例化实体**（`en_cl_fix_round / saturate / resize`），把组合逻辑包进带 `clk/rst` 的外壳，并按需插入寄存器。三个组件刻意采用了**完全一致的接口形状**，这样你可以在数据通路里把它们像积木一样替换、级联。

#### 4.1.2 核心流程

一个组件实例的生命周期：

1. **综合期**：根据 generics（输入/输出格式、舍入/饱和模式、寄存器模式）求出 `use_reg_c`（要不要插寄存器）。
2. **elaboration**：`g_register` 与 `g_no_register` 两条 `generate` 二选一，只保留一条。
3. **运行期**：
   - 组合分支：`out_*` 直接等于计算结果，延迟 0 拍。
   - 寄存器分支：每个 `rising_edge(clk)` 把 `result / in_valid / in_meta` 打一拍输出，延迟 1 拍。

数据流（寄存器分支）：

```
in_data ──► cl_fix_round(组合) ──► result ──►[reg]──► out_data
in_valid ───────────────────────────────────►[reg]──► out_valid   (and not rst)
in_meta  ───────────────────────────────────►[reg]──► out_meta
```

#### 4.1.3 源码精读

先看 round 组件的实体 generics 与端口，这是三个组件共用的接口范型：

[hdl/en_cl_fix_round.vhd:50-78](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L50-L78) — 定义 `en_cl_fix_round` 实体。generics 里 `in_fmt_g / out_fmt_g` 是输入输出定点格式，`round_g` 是舍入模式，`reg_mode_g` 是寄存器策略，`meta_width_g` 默认 0（不使用边带），`fmt_check_g` 默认 true（开启格式契约检查）。

注意端口里位宽是用纯函数**在综合期算出来**的：

```vhdl
in_data  : in  std_logic_vector(cl_fix_width(in_fmt_g)-1  downto 0);
out_data : out std_logic_vector(cl_fix_width(out_fmt_g)-1 downto 0);
```

也就是说，实体声明本身就把格式 → 位宽的推导做完了，使用者只需给格式，不必手算位宽。`in_meta` 的范围是 `meta_width_g-1 downto 0`，当 `meta_width_g = 0` 时这是 **null range**（空向量），自然「不用 meta」。

saturate 组件的实体几乎一字不差，只把 `round_g` 换成 `saturate_g`、去掉 `fmt_check_g`：

[hdl/en_cl_fix_saturate.vhd:50-57](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_saturate.vhd#L50-L57) — saturate 组件的 generics，形状与 round 组件一致。

resize 组件的实体则同时需要 `round_g` 和 `saturate_g`（因为它内部要做两件事）：

[hdl/en_cl_fix_resize.vhd:50-58](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd#L50-L58) — resize 组件的 generics，多了 `saturate_g`。

三个实体的**端口部分完全相同**（都是 `clk/rst/in_valid/in_meta/in_data/out_valid/out_meta/out_data`），这是「积木化」的关键。

#### 4.1.4 代码实践

**实践目标**：确认三个组件的端口签名是否真的逐字一致。

**操作步骤**：

1. 打开 `hdl/en_cl_fix_round.vhd`、`hdl/en_cl_fix_saturate.vhd`、`hdl/en_cl_fix_resize.vhd` 三个文件的 `port(...)` 段。
2. 逐行对照 `clk / rst / in_valid / in_meta / in_data / out_valid / out_meta / out_data` 八个端口的方向、类型、位宽表达式。
3. 单独记录 generics 的差异：哪个组件有 `fmt_check_g`？哪个同时有 `round_g` 和 `saturate_g`？

**需要观察的现象**：端口段三者一致；差异只出现在 generics——round 有 `round_g + fmt_check_g`，saturate 有 `saturate_g`，resize 有 `round_g + saturate_g`。

**预期结果**：你会得到一张「同接口、不同 generic」的对照表，这正是后面 `resize` 能直接复用 `round`/`saturate` 子组件的前提。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `meta_width_g` 设为 0，`in_meta` 这根线会发生什么？
**答案**：其范围 `(meta_width_g-1 downto 0)` 即 `(-1 downto 0)`，是 VHDL 的 null range（空数组），等价于这根线不存在；`out_meta` 同理，因此「不用 meta」时无需特殊处理。

**练习 2**：为什么 `in_data` 的位宽写成 `cl_fix_width(in_fmt_g)-1 downto 0` 而不是写死一个数字？
**答案**：因为位宽由格式 `[S,I,F]` 决定（`S+I+F`），用纯函数在综合期计算，使用者只需给格式即可，避免手算出错，也保证 `in_data` 与 `in_fmt_g` 永远一致。

---

### 4.2 RegisterMode_t：用户如何表达寄存器意愿

#### 4.2.1 概念说明

组件知道「要不要插寄存器」由 generic `reg_mode_g` 决定，它的类型是 `RegisterMode_t`——一个三值枚举。这个类型**只控制流水线组件是否插寄存器，不参与任何数值计算**（和 `FixRound_t`、`FixSaturate_t` 是完全不同的维度）。

三种模式代表三种不同的设计取舍：

| 模式 | 含义 | 延迟 | 典型用途 |
| --- | --- | --- | --- |
| `Auto_s` | 只在「需要」时插寄存器（按推荐值） | 0 或 1（resize 最多 2） | 接受延迟随 generic 变化、追求最小面积 |
| `Yes_s` | 永远插寄存器 | 1（resize 为 2） | 需要延迟恒定、不随其它 generic 改变 |
| `No_s` | 永远不插寄存器 | 0 | 极少用，会恶化时序 |

#### 4.2.2 核心流程

三者关系可以画成一张决策图：

```
                reg_mode_g
              ┌─────┼─────┐
           Auto_s  Yes_s  No_s
              │     │     │
        看 recommended_c  │
         (>0 则插)        │
              │     │     │
        0 或 1 拍       1 拍   0 拍（组合）
```

关键直觉：`Auto_s` 是「聪明模式」——它问库「这个具体配置下，组合逻辑到底有没有实质运算？」；`Yes_s` 是「保险模式」——不管有没有运算，都打一拍，换来**延迟恒定**；`No_s` 几乎是个陷阱——看似省了一拍，实则把长组合路径暴露给时序分析。

#### 4.2.3 源码精读

类型定义在包里，注释把三种模式的意图说得非常清楚：

[hdl/en_cl_fix_pkg.vhd:68-73](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L68-L73) — `RegisterMode_t` 枚举：`Auto_s`（插入推荐寄存器）、`Yes_s`（全插，利于延迟一致）、`No_s`（不插，慎用）。

三个组件文件头部的描述块也复述了同样的延迟约定，例如 round 组件：

[hdl/en_cl_fix_round.vhd:27-35](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L27-L35) — 描述 `Auto_s / Yes_s / No_s` 三种模式下 round 组件的延迟（分别为 recommended、1、0）。

注意 resize 组件的描述里 `Yes_s` 对应 **Latency = 2**（不是 1），因为它内部级联了两个子组件：

[hdl/en_cl_fix_resize.vhd:27-35](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd#L27-L35) — resize 组件 `Yes_s` 延迟为 2，因为 `cl_fix_recommended_pipelining(in_fmt_g, out_fmt_g, round_g, saturate_g)` 上限是 2。

#### 4.2.4 代码实践

**实践目标**：在测试台里观察 `reg_mode_g` 是如何被穷举覆盖的。

**操作步骤**：

1. 打开 `tb/cl_fix_round_tb.vhd`，定位 UUT 例化处。

[tb/cl_fix_round_tb.vhd:152-159](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L152-L159) — 测试台例化 `en_cl_fix_round`，其中 `reg_mode_g => RegisterMode_t'val(i mod reg_mode_count_c)`。

2. 注意 `reg_mode_count_c` 的定义在 [tb/cl_fix_round_tb.vhd:66](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/tb/cl_fix_round_tb.vhd#L66)（用 `RegisterMode_t'pos(high)` 算出枚举元素个数）。
3. 理解 `'val(i mod N)` 让不同测试用例轮流取 `Auto_s / Yes_s / No_s`。

**需要观察的现象**：测试台并不为三种寄存器模式写三份代码，而是用 `mod` 运算在 `generate` 循环里自动轮换。

**预期结果**：你应能解释「为什么一套 testbench 就能覆盖三种 `reg_mode_g`」——因为枚举的 `'val/'pos` 属性让模式可以像整数一样轮转。

#### 4.2.5 小练习与答案

**练习 1**：为什么注释说 `No_s`「通常是个坏选择」？
**答案**：因为不插寄存器意味着把组件内部的组合逻辑直接暴露给上一级/下一级，组合路径变长，时序性能（最高时钟频率）通常变差；只有在你能保证整体路径仍然满足时序、且确实需要 0 延迟时才用。

**练习 2**：`RegisterMode_t` 会影响 `out_data` 的**数值**吗？
**答案**：不会。它只影响是否在输出端打一拍寄存器（时序/延迟），数值结果由 `round_g / saturate_g / in_fmt_g / out_fmt_g` 决定，与 `reg_mode_g` 无关。

---

### 4.3 cl_fix_recommended_pipelining：库如何判断「需不需要一拍」

#### 4.3.1 概念说明

`Auto_s` 模式的「聪明」来自一个纯函数：`cl_fix_recommended_pipelining`。它回答一个问题——**在给定的格式与模式下，组合逻辑里到底有没有「实质运算」？** 如果没有（比如纯截断、或根本没丢小数位），就返回 0，组件就不插寄存器；如果有（需要加偏移、需要比较钳位），就返回 1。

这个函数有**三个重载**，分别对应 round、saturate、resize 三类操作。它们的返回类型都是 `natural`，且取值只有 0 或 1（resize 重载是两个 0/1 相加，所以是 0/1/2）。

#### 4.3.2 核心流程

三个重载的判定逻辑可以归纳为「**两种零逻辑情形**」：

**round 重载**——以下两种情况返回 0（不需要寄存器）：
1. 模式是 `Trunc_s`（纯截断，不加偏移，没有任何额外逻辑）。
2. `result_fmt.F >= a_fmt.F`（没有减少小数位，即没有舍入发生，只是低位补零）。
其余情况返回 1。

**saturate 重载**——以下两种情况返回 0：
1. 模式是 `None_s` 或 `Warn_s`（只回绕/只告警，不做钳位比较）。
2. `result_fmt.I >= a_fmt.I` 且 `result_fmt.S = a_fmt.S`（没有减少整数位也没有改变符号位，不可能越界，钳位逻辑退化）。
其余情况返回 1。

**resize 重载**——把 resize 拆成「round 步 + saturate 步」，分别调用上面两个重载再相加：

\[
\text{recommended}_{\text{resize}} = \text{recommended}_{\text{round}}(a \to r_{\text{fmt}}) + \text{recommended}_{\text{sat}}(r_{\text{fmt}} \to \text{result})
\]

其中 \( r_{\text{fmt}} = \text{cl\_fix\_round\_fmt}(a, \text{result}.F, \text{round}) \) 是舍入后的中间格式（可能比 result 多 1 个整数位，见 u3-l3）。

#### 4.3.3 源码精读

三个重载的声明（按 round/saturate/resize 顺序）在包头：

[hdl/en_cl_fix_pkg.vhd:164-185](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L164-L185) — 三个 `cl_fix_recommended_pipelining` 重载声明，分别针对 round、saturate、resize。

round 重载的实现，逻辑非常直白：

```vhdl
if round = Trunc_s then
    return 0;                       -- (1) 截断无需逻辑
else
    assert ... 非截断模式 ...;
end if;
if result_fmt.F >= a_fmt.F then
    return 0;                       -- (2) 没减少小数位
end if;
return 1;
```

[hdl/en_cl_fix_pkg.vhd:1041-1069](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1041-L1069) — round 重载实现。注意开头的 `assert result_fmt = cl_fix_round_fmt(...)` 是「格式契约」：它强制 `result_fmt` 必须是合法的舍入结果格式（否则让你改用 `cl_fix_round_fmt()`）。`fmt_check` 参数允许谨慎地关掉这个检查。

saturate 重载实现结构对称：

[hdl/en_cl_fix_pkg.vhd:1071-1097](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1071-L1097) — saturate 重载实现。开头 `assert result_fmt.F = a_fmt.F`（饱和期小数位不许变，呼应 u5-l2 的「饱和要求 F 不变」），然后按「回绕模式 → 0」「整数位/符号位没减少 → 0」「否则 → 1」判定。

resize 重载实现只有三行，却把 u5-l2「resize = round 后 saturate」的核心体现了出来：

[hdl/en_cl_fix_pkg.vhd:1099-1109](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1099-L1109) — resize 重载实现：算出中间舍入格式 `round_fmt_c`，再返回「round 段推荐值 + saturate 段推荐值」。这正是 resize 组件延迟上限为 2 的来源。

#### 4.3.4 代码实践

**实践目标**：手工推演几组配置下 `cl_fix_recommended_pipelining` 的返回值。

**操作步骤**：对 round 重载，填写下表（`a_fmt → result_fmt`，模式 `round`）：

| 配置 | 是否 Trunc? | result.F ≥ a.F? | 推荐值 |
| --- | --- | --- | --- |
| `[0,4,4] → [0,4,1]`, NonSymPos_s | 否 | 否（1<4） | ? |
| `[0,4,4] → [0,4,8]`, NonSymPos_s | 否 | 是（8≥4） | ? |
| `[0,4,4] → [0,4,1]`, Trunc_s | 是 | — | ? |

**需要观察的现象**：对照 [hdl/en_cl_fix_pkg.vhd:1056-1068](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_pkg.vhd#L1056-L1068) 的两个判定，逐行验证你的推断。

**预期结果**：三行的推荐值分别为 **1、0、0**。第一行「真舍入」需要一拍；第二行「只补零不丢位」不需要；第三行「纯截断」不需要。

#### 4.3.5 小练习与答案

**练习 1**：saturate 重载里，为什么「`result_fmt.I >= a_fmt.I` 且 `result_fmt.S = a_fmt.S`」时返回 0？
**答案**：因为饱和只在「整数位或符号位被减少」时才可能越界。如果两者都没变（或整数位还变多了），输入值天然落在输出范围内，钳位比较逻辑不会触发，等价于没有实质运算，故不需要寄存器。

**练习 2**：resize 重载的返回值可能是 2，这要求 round 段和 saturate 段都返回 1。请举一个这样的例子。
**答案**：例如 `[1,8,8] → [1,4,2]`，`round=NonSymPos_s`，`saturate=SatWarn_s`。round 段减少小数位（8→2，且非 Trunc）→ 1；中间舍入格式到 result 减少了整数位（且 SatWarn 钳位）→ 1；合计 2。

---

### 4.4 use_reg_c 与 g_register / g_no_register：编译期二选一

#### 4.4.1 概念说明

`cl_fix_recommended_pipelining` 只给出「推荐值」（0 或 1），真正决定「插不插寄存器」的是组件体里的布尔常量 `use_reg_c`。它把用户的 `reg_mode_g` 和库的推荐值**合并**成一个布尔决策：

\[
\text{use\_reg} = (\text{reg\_mode} = \text{Yes}) \;\lor\; \big(\text{reg\_mode} = \text{Auto} \;\land\; \text{recommended} > 0\big)
\]

也就是说：`Yes_s` 无条件插；`Auto_s` 看推荐值；`No_s` 既不满足 `Yes` 也不满足 `Auto`，结果为假，不插。

这个布尔值喂给两条互斥的 `generate` 分支，在 elaboration 期只活下来一条——这就是「综合期二选一」。

#### 4.4.2 核心流程

```
recommended_c = cl_fix_recommended_pipelining(...)      -- 0 或 1
use_reg_c      = (reg_mode_g=Yes_s) or (Auto_s and recommended_c>0)
                                                          │
                       ┌──────────────────────────────────┤
                   use_reg_c                              not use_reg_c
                       │                                     │
                g_register                             g_no_register
            (process(clk) 打一拍)                      (纯组合直连)
            延迟 1 拍                                  延迟 0 拍
```

**回答规格里的核心问题**：当 `reg_mode_g = Auto_s` 且舍入实际无需寄存器（`recommended_c = 0`，比如 `Trunc_s` 或没减少小数位），`use_reg_c = (No) or (Yes and False) = False`，于是 `g_no_register` 分支胜出，组件是**纯组合、0 拍延迟**。这就是 `Auto_s`「自动选择 0 拍」的完整链路。

#### 4.4.3 源码精读

round 组件体里的两个常量定义：

[hdl/en_cl_fix_round.vhd:85-86](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L85-L86) — `recommended_c`（推荐寄存器数，`natural range 0 to 1`）与 `use_reg_c`（最终布尔决策）。注意 `recommended_c` 的子类型约束 `range 0 to 1` 是编译期护栏。

组合结果先算出来，再由两条分支决定是否打拍：

[hdl/en_cl_fix_round.vhd:92-111](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_round.vhd#L92-L111) — `result <= cl_fix_round(...)` 是共用计算；`g_register` 分支用 `process(clk)` 把 `result/in_valid/in_meta` 打一拍；`g_no_register` 分支把它们直接连到输出。

注意 `out_valid <= in_valid and not rst;`——寄存器分支里，valid 被 `rst` 同步清零（复位期间输出无效），这是标准的同步复位 valid 处理。saturate 组件体逐字相同：

[hdl/en_cl_fix_saturate.vhd:84-110](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_saturate.vhd#L84-L110) — saturate 组件体的常量与两条 generate 分支，与 round 组件几乎一字不差，只换内部纯函数与 generic。

#### 4.4.4 代码实践

**实践目标**：对照三个组件，确认它们的结构几乎一致；并验证 `Auto_s` 在无需寄存器时如何落到 0 拍。

**操作步骤**：

1. 打开 `en_cl_fix_round.vhd` 与 `en_cl_fix_saturate.vhd` 的 architecture 体，逐行比对第 83–112 行。
2. 你会发现除了 `recommended_c` 调用的重载不同（一个传 `round_g`，一个传 `saturate_g`）、`result <=` 调用的纯函数不同（`cl_fix_round` vs `cl_fix_saturate`），**其余完全相同**。
3. 构造一个 `Auto_s` + 无需寄存器的场景：`in_fmt_g = out_fmt_g = (0,4,4)`，`round_g = Trunc_s`。手工代入：`recommended_c` = ?，`use_reg_c` = ?，哪条 `generate` 胜出？

**需要观察的现象**：两个组件体是「同构」的；在上述场景里 `recommended_c = 0`（Trunc_s），`use_reg_c = (Yes_s?) 否 or (Auto_s and 0>0) = 假`，`g_no_register` 胜出，延迟 0。

**预期结果**：你应能得出——「`Auto_s` + 截断/不丢位 → 自动 0 拍」，这正是「自动选择 0 拍延迟」的完整解释。若把同一实例的 `reg_mode_g` 改成 `Yes_s`，则 `use_reg_c = 真`，强制 1 拍——这是 `Auto_s` 与 `Yes_s` 的本质差别。

> 待本地验证：如需眼见为实，可在仿真器里跑 `tb/cl_fix_round_tb`，观察 `reg_mode_g` 取 `Auto_s`（截断用例）时输出与输入**同拍**出现（0 延迟），而取 `Yes_s` 时输出**滞后一拍**。

#### 4.4.5 小练习与答案

**练习 1**：`use_reg_c` 是 `constant ... boolean`，为什么用常量而不是信号？
**答案**：因为它在 elaboration 期就完全确定（只依赖 generics 与纯函数），用常量可以让 `if use_reg_c generate` 在综合前就被求值，确保只有一条分支被综合进网表，避免生成死逻辑。

**练习 2**：`out_meta` 在寄存器分支里为什么也要进 `process(clk)`？
**答案**：因为 meta 是数据的边带信息（如通道号、标签），必须与数据**同拍**到达输出。如果数据打了一拍而 meta 没打，meta 就会和错位的数据对不上，所以 meta 必须和数据一起穿过同一级寄存器。

---

### 4.5 resize 组件：级联 round + saturate

#### 4.5.1 概念说明

`resize` 在纯函数层就是「先 round 后 saturate」（u5-l2）。到了组件层，库**没有重写一遍**这个组合，而是直接**例化一个 `en_cl_fix_round` 子组件 + 一个 `en_cl_fix_saturate` 子组件**，把它们串起来。这是很好的工程复用：三个组件里 round 和 saturate 是「叶子」，resize 是「组合」，逻辑只维护两份。

这种级联也决定了 resize 的延迟模型：每个子组件最多 1 拍，所以 resize 最多 2 拍。

#### 4.5.2 核心流程

```
            ┌─────────────┐        ┌─────────────────┐
in_data ──► │ en_cl_fix_  │ round_ │  en_cl_fix_     │ ──► out_data
in_meta ──► │ round       │ data/  │  saturate       │ ──► out_meta
in_valid─►► │ (in→round   │ meta/  │  (round→out     │ ──► out_valid
            │  fmt)       │ valid ─►   fmt)          │
            └─────────────┘        └─────────────────┘
              延迟 0/1                 延迟 0/1
```

关键细节：round 子组件的 `out_fmt_g` **不是** resize 的 `out_fmt_g`，而是中间舍入格式 `round_fmt_c = cl_fix_round_fmt(in_fmt_g, out_fmt_g.F, round_g)`。这是因为舍入后格式可能比目标格式多 1 个整数位（u3-l3 讲过的 +1 进位），饱和必须在「舍入后的真实格式」上做，否则会错。这与 u5-l2「resize 内部自动推导 round_fmt 并处理 +1」完全对应。

#### 4.5.3 源码精读

resize 组件体先算出中间格式常量，再例化两个子组件：

[hdl/en_cl_fix_resize.vhd:85-89](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd#L85-L89) — 定义中间舍入格式 `round_fmt_c` 及级联用的内部信号（`round_valid/round_meta/round_data`），`round_data` 的位宽按 `round_fmt_c` 计算。

round 子组件的例化，注意 `out_fmt_g => round_fmt_c` 和 `reg_mode_g => reg_mode_g`（透传）：

[hdl/en_cl_fix_resize.vhd:96-116](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd#L96-L116) — 例化 `en_cl_fix_round`，把 `in_fmt_g → round_fmt_c`，并把 `reg_mode_g` 原样传给子组件。

saturate 子组件的例化，输入是 round 子组件的输出，输出是 resize 的最终输出：

[hdl/en_cl_fix_resize.vhd:121-141](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/hdl/en_cl_fix_resize.vhd#L121-L141) — 例化 `en_cl_fix_saturate`，把 `round_fmt_c → out_fmt_g`，同样透传 `reg_mode_g`。

这里有一个微妙之处值得强调：resize 把 `reg_mode_g` **同时**透传给两个子组件。所以：

- `reg_mode_g = Yes_s`：两个子组件各插一拍 → resize 延迟 = 2（与文件头注释一致）。
- `reg_mode_g = Auto_s`：每个子组件各自按推荐值决定 → resize 延迟 = recommended_round + recommended_sat（0、1 或 2）。
- `reg_mode_g = No_s`：两个都不插 → 延迟 0。

#### 4.5.4 代码实践

**实践目标**：跟踪一个 resize 实例的「格式与延迟」全过程。

**操作步骤**：

1. 设想实例 `in_fmt_g = (1,8,8)`，`out_fmt_g = (1,4,2)`，`round_g = NonSymPos_s`，`saturate_g = SatWarn_s`，`reg_mode_g = Auto_s`。
2. 手算 `round_fmt_c = cl_fix_round_fmt((1,8,8), 2, NonSymPos_s)`：F=2，非 Trunc 故整数位 +1 → `I = 8+1 = 9`，符号位仍 1 → `(1,9,2)`（**待本地验证**：可用 Python `FixFormat.for_round` 对照）。
3. round 子组件：`(1,8,8) → (1,9,2)`，NonSymPos 且减少小数位 → recommended = 1。
4. saturate 子组件：`(1,9,2) → (1,4,2)`，SatWarn 且整数位减少 → recommended = 1。
5. 因此 `Auto_s` 下 resize 总延迟 = 1 + 1 = 2。

**需要观察的现象**：round 子组件的输出格式比 `out_fmt_g` 多了 1 个整数位（`(1,9,2)` vs `(1,4,2)`），这正是 saturate 子组件输入格式的来源——验证了「饱和必须基于舍入后的格式」。

**预期结果**：你得到一条「`(1,8,8)` → round → `(1,9,2)` → saturate → `(1,4,2)`」的两级流水线，`Auto_s` 下延迟为 2，`Yes_s` 下也是 2，`No_s` 下为 0。若把 `round_g` 改成 `Trunc_s`，round 段 recommended 变 0，`Auto_s` 下总延迟降为 1。

#### 4.5.5 小练习与答案

**练习 1**：为什么 resize 组件不直接写一个大的 `process(clk)` 把 round 和 saturate 都打一拍，而要例化两个子组件？
**答案**：为了复用——round 和 saturate 子组件已经各自封装了「组合逻辑 + 可选寄存器 + valid/meta 旁路」的全部细节，resize 直接级联它们即可，不必重写；同时也让延迟模型清晰（每段独立 0/1 拍），并保证 meta/valid 在两级之间正确穿透。

**练习 2**：若 `reg_mode_g = Auto_s` 且 round 段需要寄存器、saturate 段不需要，resize 的总延迟是多少？meta 会在哪一级被打拍？
**答案**：总延迟 = 1 + 0 = 1。round 子组件插了寄存器（meta 在这一级被打拍），saturate 子组件走组合直连（meta 直接透传），所以 meta 只在 round 这一级被延迟一拍，与数据保持同步。

---

## 5. 综合实践

**任务**：为「乘法 → 舍入 → 饱和」定点通路画一份组件级设计草图，并推导各级格式与 `Auto_s` 下的延迟。

背景：假设乘法器已用 `cl_fix_mult`（纯函数，见 u5-l3）实现，输出格式 `mult_fmt = cl_fix_mult_fmt(a_fmt, b_fmt)`。现在你要把它的结果舍入并饱和到最终输出格式 `out_fmt`。

要求：

1. **画出数据通路**：`mult 结果 → en_cl_fix_round → en_cl_fix_saturate`（或直接用 `en_cl_fix_resize`）。标注 `clk/rst`、`valid` 与 `meta` 如何贯穿各级。
2. **推导格式**：设 `a_fmt = (1,4,4)`、`b_fmt = (1,4,4)`、`out_fmt = (1,5,2)`、`round = NonSymPos_s`、`saturate = SatWarn_s`。
   - 用 `cl_fix_mult_fmt` 算 `mult_fmt`（提示：两个 `[1,4,4]` 相乘，F=8，整数位与符号位见 u3-l2）。
   - 若用 `en_cl_fix_resize`，写出它内部 round 子组件的 `out_fmt_g`（即 `round_fmt_c`）与 saturate 子组件的 `in_fmt_g`。
3. **推导延迟**：在 `reg_mode_g = Auto_s` 下，分别用「round 组件 + saturate 组件」与「单个 resize 组件」两种方案，计算总延迟，并说明它们是否相等。
4. **决策**：如果你的系统要求**延迟恒定**（不随 `round_g/saturate_g` 变化），应该选 `Auto_s` 还是 `Yes_s`？分别给出两种方案下的恒定延迟值。

**参考思路**：

- 两个 `[1,4,4]` 相乘：F = 4+4 = 8；两个有符号数相乘，最负值 × 最负值 = 2 的幂，整数位 +1，符号位按「1 位有符号 × 1 位有符号」特例——此处非 1 位，结果仍为有符号。综合得 `mult_fmt` 大致为 `(1,9,8)`（**待本地验证**，可用 Python `FixFormat.for_mult` 对照）。
- resize 内部 `round_fmt_c = cl_fix_round_fmt(mult_fmt, out_fmt.F=2, NonSymPos_s)` → F=2、整数位 +1。
- 「round 组件 + saturate 组件」与「单个 resize 组件」在 `Auto_s` 下延迟**相等**（resize 就是前者的封装）；但若你手动控制，round 组件可单独设 `Yes_s`、saturate 设 `Auto_s`，从而得到不同于「全 Auto」的延迟组合——这是拆分方案带来的额外灵活性。
- 要求恒定延迟 → 选 `Yes_s`：拆分方案恒为 2（各 1 拍），resize 方案恒为 2。

> 待本地验证：格式推导建议用 Python 参考模型 `FixFormat.for_mult / for_round`（u3-l2、u3-l3）核对，避免手算「2 的幂边界」出错。

## 6. 本讲小结

- 三个组件 `en_cl_fix_round / saturate / resize` 共享**完全相同的端口接口**（`clk/rst/valid/meta/data`），差异只在 generics，因此可以像积木一样替换、级联。
- `RegisterMode_t`（`Auto_s / Yes_s / No_s`）是**纯时序控制**，不影响数值，只决定是否插寄存器；`Yes_s` 换恒定延迟，`Auto_s` 换最小面积，`No_s` 几乎不用。
- `cl_fix_recommended_pipelining` 的**三个重载**是 `Auto_s` 的大脑：round 段看「非 Trunc 且减少小数位」、saturate 段看「钳位模式且减少整数/符号位」，resize 段是两者之和。
- 布尔常量 `use_reg_c = (Yes_s) or (Auto_s and recommended>0)` 驱动 `g_register / g_no_register` 两条 `generate` 在综合期二选一；`Auto_s` 且无需寄存器时自动落到 0 拍组合逻辑。
- `resize` 组件不重写逻辑，而是**级联**一个 round 子组件和一个 saturate 子组件，中间格式用 `cl_fix_round_fmt` 推导，延迟上限为 2。
- valid 与 meta 必须与数据**同拍**穿过每一级寄存器（`out_valid <= in_valid and not rst`），保证边带信息不错位。

## 7. 下一步学习建议

- **下一步本单元**：进入 u6-l2「搭建流水线数据通路」，学习如何把本讲的三个组件与乘法器级联成完整定点通路，用 `meta_width_g` 透传边带、用 `Yes_s` 保证跨配置的固定延迟。
- **验证视角**：本讲反复出现「延迟可推导」，下一篇可回到 u7（Python↔HDL 协同仿真），看 cosim 如何在不同 `reg_mode_g` 与格式组合下逐拍比对 UUT 输出，验证这些延迟与数值结论。
- **源码延伸阅读**：通读 `hdl/en_cl_fix_pkg.vhd` 中 `cl_fix_recommended_pipelining` 的三个重载（1041–1109 行），并对照 `cl_fix_round_fmt`（99 行声明）理解「舍入后中间格式」如何在 resize 组件里被推导与使用。

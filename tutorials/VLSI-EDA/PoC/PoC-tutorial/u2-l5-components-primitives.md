# components 包：用函数描述原语

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「把触发器写成函数」这个设计思路到底妙在哪里，以及为什么函数本身不存状态、寄存器是怎样「长出来的」。
- 看懂 `ffdre`/`ffdse`/`ffrs`/`fftre` 等触发器函数的参数表，并能在自己的进程里用一行代码例化一个带使能与复位的寄存器。
- 用 `mux` 系列函数实现二选一选择，并理解 PoC 还把计数器、移位寄存器、比较器也封装成了一行可调用的函数。
- 牢记一个最容易踩坑的细节：`INIT` 参数是 **复位值**，不是上电初值；上电初值必须写在信号声明里。

## 2. 前置知识

本讲是 [u2-l1（公共包总览）](u2-l1-common-packages-overview.md) 与 [u2-l2（utils 包）](u2-l2-utils-package.md) 的直接续篇，开始之前请确认你理解下面三件事：

1. **`components` 不在 `context Common` 套餐里。** 哪怕你已经写了 `context PoC.common;`，只要用到触发器、选择器这类原语，就必须**单独**再补一句 `use PoC.components.all;`。这一点在 u2-l1 已经讲过，本讲不再重复。
2. **`SIMULATION` 常量。** 它是 `utils` 包里一个延迟常量（声明见 [utils.vhdl:45](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L45)，定义见 [utils.vhdl:334](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L334)），仿真时为 `true`、综合时为 `false`。`components` 里几乎所有触发器函数都靠它在「仿真模型」和「可综合逻辑」之间切换。
3. **`ite` 与 `to_sl`。** `ite(cond, a, b)` 是 `utils` 里被重载了十几种类型的事实三元运算符（布尔版定义见 [utils.vhdl:556](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L556)）；`to_sl(bool)` 把布尔转成 `std_logic`（[utils.vhdl:891](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl#L891)）。本讲的 `registered` 与比较函数都会用到它们。

另外，本讲假设你能读懂最基本的 VHDL 时钟进程（`signal q; q <= d when rising_edge(Clock);`）。

## 3. 本讲源码地图

本讲只围绕一个核心源码文件展开，但会引用一个真实的调用方作为示范。

| 文件 | 作用 | 依赖 |
| --- | --- | --- |
| [src/common/components.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl) | 把触发器、计数器、移位寄存器、比较器、多路选择器等「原语」写成一行可调用的函数 | `PoC.utils` |
| [src/sort/sortnet/sortnet_OddEvenMergeSort.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sort/sortnet/sortnet_OddEvenMergeSort.vhdl) | 排序网络核，真实示范 `ffdre` + `mux` 如何搭配使用 | `PoC.components`、`PoC.utils` 等 |
| [src/common/utils.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/utils.vhdl) | 提供 `SIMULATION`、`ite`、`to_sl` 等本讲会引用的辅助定义 | 标准库 |

`components.vhdl` 的文件头就开宗明义地说明了它的设计意图，也埋下了本讲最重要的一颗「雷」——`INIT` 参数：

> The parameter 'constant INIT' of some functions is actually the reset value, not the initial value after device programming (e.g. for FPGAs), this value MUST be set via signal declaration!

详见 [components.vhdl:14-17](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L14-L17)。这句话我们会在 4.3 节彻底拆开。

---

## 4. 核心概念与源码讲解

### 4.1 触发器函数

#### 4.1.1 概念说明

写 VHDL 时，一个带「同步复位 + 使能」的 D 触发器进程，传统写法大概长这样：

```vhdl
process(Clock) begin
  if rising_edge(Clock) then
    if rst = '1' then
      q <= '0';
    elsif en = '1' then
      q <= d;
    end if;
  end if;
end process;
```

只要你想多写几个这样的寄存器，就要把这段 `if/elsif` 重复一遍又一遍。`components` 包的做法是：**把这段「下一拍该取什么值」的组合逻辑提炼成一个函数，函数名就叫 `ffdre`**（D-FlipFlop with reset and enable）。

但这里有一个 VHDL 语法上的关键限制必须先想明白：**VHDL 的 function 没有时钟、没有状态、不能在内部等待边沿**。所以触发器不可能真的「住在」函数里。`components` 的妙处在于它换了个角度看问题：

> 触发器 = 「计算下一态的组合函数」 + 「一个时钟边沿上的信号自反馈」。

函数只负责回答「在当前 `q`、`d`、`rst`、`en` 之下，下一拍的 `q` 应该是多少」；而真正把它变成寄存器的，是调用方那一行 `... when rising_edge(Clock)` 的时钟赋值。寄存器是「长」在调用现场、而不是长在函数里的。

这样一来，每加一个寄存器，调用方只需要写一行；复位/使能/保持的细节被集中维护在函数体内，全库共用。

#### 4.1.2 核心流程

`ffdre` 计算的「下一态」逻辑可以写成一条布尔等式。以默认 `INIT='0'` 的可综合分支为例：

\[
q^{+} = \bigl((d \cdot en) + (q \cdot \overline{en})\bigr) \cdot \overline{rst}
\]

这条公式读出来就是三句话：

- **使能 `en=1` 时**：括号里选中 `d`，即「载入新数据」。
- **使能 `en=0` 时**：括号里选中 `q`，即「保持原值」。
- **复位 `rst=1` 时**：整个结果再 `and not rst`，被强制清 0（也就是回到 `INIT` 的值）。

也就是说，`en` 本质上是一个内嵌的 2 选 1 选择器，`rst` 是一个在最后一步生效的「盖帽子」。这个观察会在 4.2 节再次出现——`mux` 函数和这里的 `(d and en) or (q and not en)` 其实是同一套布尔逻辑。

调用侧的固定套路是：

```vhdl
signal q : std_logic := '0';          -- 上电初值写在这里（见 4.3）
...
q <= ffdre(q => q, d => d, en => en, rst => rst) when rising_edge(Clock);
```

整个流程拆成三步：

1. 右边的 `q` 是「当前值」，连同 `d/en/rst` 一起喂给 `ffdre`。
2. `ffdre` 返回「下一态值」。
3. `when rising_edge(Clock)` 让这个下一态值在时钟上升沿写回 `q`——寄存器就此诞生。

#### 4.1.3 源码精读

先看 `ffdre` 标量版的声明与函数体，见 [components.vhdl:55](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L55)（声明）与 [components.vhdl:106-122](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L106-L122)（实现）。函数体把仿真与综合分成两条路：

```vhdl
function ffdre(q : std_logic; d : std_logic; rst : std_logic := '0';
               en : std_logic := '1'; constant INIT : std_logic := '0') return std_logic is
begin
  if not SIMULATION then                                   -- 综合路径
    if (INIT = '0') then
      return ((d and en) or (q and not en)) and not rst;    -- 复位清 0
    elsif (INIT = '1') then
      return ((d and en) or (q and not en)) or rst;         -- 复位置 1
    else
      report "Unsupported INIT value for synthesis." severity FAILURE;
      return 'X';
    end if;
  elsif (rst = '1') then                                    -- 仿真路径：复位直接给 INIT
    return INIT;
  else
    return ((d and en) or (q and not en));
  end if;
end function;
```

这段代码说明了几件事：

- **`INIT` 决定复位的「方向」**：`INIT='0'` 时，复位用 `and not rst` 把输出压到 0；`INIT='1'` 时换成 `or rst` 把输出抬到 1。所以同一个函数既能描述「复位到 0」也能描述「复位到 1」的寄存器。
- **`en` 默认是 `'1'`、`rst` 默认是 `'0'`**：所以最简调用 `ffdre(q, d)` 就是一个无复位、常使能的普通 D 触发器。
- **仿真与综合不一致是有意为之**：仿真路径在 `rst='1'` 时直接返回 `INIT`，更像行为模型；综合路径则把复位融进纯布尔表达式，便于工具推断成硬件触发器。两者在外部观察到的「复位行为」是一致的。

`ffdre` 还有一个**向量版**重载，见 [components.vhdl:124-132](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L124-L132)。它先用 `resize` 把 `INIT` 对齐到 `q` 的位宽，再逐位调用标量版 `ffdre`：

```vhdl
function ffdre(q : std_logic_vector; d : std_logic_vector; rst : std_logic := '0';
               en : std_logic := '1';
               constant INIT : std_logic_vector := (0 to 0 => '0')) return std_logic_vector is
  constant INIT_I : std_logic_vector(q'range) := resize(INIT, q'length);
  variable Result : std_logic_vector(q'range);
begin
  for i in q'range loop
    Result(i) := ffdre(q => q(i), d => d(i), rst => rst, en => en, INIT => INIT_I(i));
  end loop;
  return Result;
end function;
```

于是 `ffdre` 对 1 比特和 N 比特都适用，调用方式完全一样——这是「一份源码、多位宽复用」的典型手段。

再看两个「靠调用 `ffdre` 来实现」的同类函数，体现了很好的复用：

- **`ffdse`**（D-FlipFlop with **set** and enable），见 [components.vhdl:135-138](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L135-L138)：把 `set` 当成 `rst` 传进去，并把 `INIT` 写死成 `'1'`。也就是说，「带置位」的触发器 = 「复位到 1」的触发器。
- **`fftre`/`fftse`**（T 触发器，翻转触发器），见 [components.vhdl:141-157](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L141-L157) 与 [components.vhdl:160-163](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L160-L163)：把 D 换成「翻转输入 `t`」，下一态公式变成 \( q^{+} = \overline{q}\cdot t + q\cdot\overline{t} \)（即 `q xor t`），`fftse` 同样靠调用 `fftre` 并置 `INIT='1'` 实现「置位版」。

最后是两个**异步**置位/复位 RS 触发器 `ffrs` / `ffsr`，见 [components.vhdl:166-175](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L166-L175)。它们的区别在「谁优先」：`ffrs` 复位优先（`(q or set) and not rst`），`ffsr` 置位优先（`(q and not rst) or set`）。注意这两个函数返回的是纯组合下一态，调用方若想要真正的 RS 锁存器，同样要靠时钟或反馈来形成存储。

> 真实调用示范：排序网络核 `sortnet_OddEvenMergeSort` 在每个比较-交换单元里，用 `ffdre` 把「是否交换」的计算结果寄存一拍，再用 `mux` 在「寄存值」和「直通值」之间二选一，见 [sortnet_OddEvenMergeSort.vhdl:180-181](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sort/sortnet/sortnet_OddEvenMergeSort.vhdl#L180-L181)：
>
> ```vhdl
> Switch_r <= ffdre(q => Switch_r, d => Switch_d, en => Switch_en) when rising_edge(Clock);
> Switch   <= mux(Switch_en, Switch_r, Switch_d);
> ```
>
> 注意它没有传 `rst` 和 `INIT`，因此复位默认为 `'0'`、`INIT` 默认为 `'0'`——复位值与上电值都是 0，正好和它信号声明里 `signal Switch_r : std_logic := '0';`（[sortnet_OddEvenMergeSort.vhdl:170](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sort/sortnet/sortnet_OddEvenMergeSort.vhdl#L170)）一致。这是一个「正确做法」的范本，4.3 节会反过来看「错误做法」。

#### 4.1.4 代码实践

**实践目标**：亲手用 `ffdre` 实现一个带使能与同步复位的单比特寄存器，验证「函数 + 时钟赋值」能综合成真正的 D 触发器。

**操作步骤**：

1. 新建一个测试用文件 `echo_ffdre.vhdl`（这是**示例代码**，不是仓库原有文件），骨架如下：

   ```vhdl
   library IEEE;
   use     IEEE.STD_LOGIC_1164.ALL;

   library PoC;
   use     PoC.components.all;          -- 记得单独引这个包

   entity echo_ffdre is
     port (Clock : in  std_logic;
           rst   : in  std_logic;
           en    : in  std_logic;
           d     : in  std_logic;
           q     : out std_logic);
   end entity;

   architecture rtl of echo_ffdre is
     signal q_i : std_logic := '0';     -- 上电初值必须写在这里（见 4.3）
   begin
     q_i <= ffdre(q => q_i, d => d, rst => rst, en => en) when rising_edge(Clock);
     q   <= q_i;
   end architecture;
   ```

2. 在你熟悉的仿真器（GHDL、ModelSim 等）里编译 `PoC` 库后，再编译这个文件，写一个最小测试台：拉高 `rst` 一个周期，再分别让 `en=0`/`en=1` 观察输出。

3. （可选）把这段代码放进综合工具（Vivado/Quartus）跑一次 RTL 综合，查看原理图。

**需要观察的现象**：

- `en=1` 时，`q` 每个上升沿跟随 `d`；`en=0` 时，`q` 保持上一拍不动。
- `rst=1` 出现后的**下一个上升沿**，`q` 变成 0（同步复位）。

**预期结果**：

- 仿真波形上 `q_i` 的行为符合上述描述。
- 综合后的 RTL 原理图里能看到一个带 `CE`（时钟使能）和 `R`（复位）的 D 触发器原语（如 Xilinx 的 `FDCE`）。

如果暂时没有可用仿真/综合环境，本实践可降级为「源码阅读型」：在 [components.vhdl:106-122](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L106-L122) 中代入 `en=0`、`rst=0`，手算 `ffdre` 返回值应为 `q`（保持）；代入 `en=1`、`rst=0` 应为 `d`（载入）。**待本地验证**实际综合原理图。

#### 4.1.5 小练习与答案

**练习 1**：把上面骨架里的调用改成 `ffdre(q => q_i, d => d, en => en)`（删掉 `rst`），复位行为会变成什么？

**答案**：`rst` 参数有默认值 `'0'`，所以删掉之后等于「永不复位」。`q_i` 只受 `en` 控制：`en=1` 载入 `d`，`en=0` 保持。综合出来的触发器将没有复位端口。

**练习 2**：用 `ffdse` 而不是 `ffdre` 实现一个「置位优先」的寄存器，调用该写成什么样？

**答案**：`q_i <= ffdse(q => q_i, d => d, set => set, en => en) when rising_edge(Clock);`。根据 [components.vhdl:135-138](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L135-L138)，`ffdse` 内部等价于 `ffdre(..., rst => set, INIT => '1')`，即 `set=1` 时把寄存器置 1。

---

### 4.2 多路选择器（及其他组合原语）

#### 4.2.1 概念说明

`components` 包并不只有触发器。它把一类**纯组合**的常用小电路也写成了函数：多路选择器 `mux`、计数器 `upcounter_*`/`downcounter_*`、移位/旋转寄存器 `shreg_*`/`rreg_*`、比较器 `comp*`。这些函数的设计动机和触发器一样：把重复的样板逻辑集中起来，让调用方一行搞定。

其中最常用的是 `mux`——一个二选一选择器。注意它的「选择信号」是单比特 `sel`：`sel=0` 选第 0 路，`sel=1` 选第 1 路。这正好和 4.1 里 `ffdre` 内部那个 `(d and en) or (q and not en)` 是同构的——本质上 `en` 就是一个内嵌的 `mux`。

#### 4.2.2 核心流程

二选一选择器的标准布尔表达是：

\[
\mathrm{out} = \mathrm{sl}_0 \cdot \overline{\mathit{sel}} \;+\; \mathrm{sl}_1 \cdot \mathit{sel}
\]

即「`sel=0` 走 `sl0`，`sel=1` 走 `sl1`」。对向量来说，只要把这个式子按位展开即可，方法是把 `sel` 复制成和向量等宽的「掩码」再按位与/或。

`components` 还提供一个看起来不起眼、但思路很巧的辅助函数 `registered`：它把「要不要插一拍寄存器」这个**配置选项**也封装成函数，让一个 generic 就能切换组合/时序两种实现。

#### 4.2.3 源码精读

`mux` 一共有 4 个重载，覆盖 `std_logic`、`std_logic_vector`、`unsigned`、`signed`。声明见 [components.vhdl:88-91](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L88-L91)。标量版与向量版的实现如下，见 [components.vhdl:309-317](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L309-L317)：

```vhdl
function mux(sel : std_logic; sl0 : std_logic; sl1 : std_logic) return std_logic is
begin
  return (sl0 and not sel) or (sl1 and sel);
end function;

function mux(sel : std_logic; slv0 : std_logic_vector; slv1 : std_logic_vector) return std_logic_vector is
begin
  return (slv0 and not (slv0'range => sel)) or (slv1 and (slv1'range => sel));
end function;
```

向量版里的 `(slv0'range => sel)` 是 VHDL 的「聚合赋值」写法，作用是把单比特 `sel` 复制成一个和 `slv0` 等宽的向量，从而让按位的 `and / or` 成立。`unsigned`/`signed` 版（[components.vhdl:319-327](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L319-L327)）写法几乎完全一样。

`registered` 函数见 [components.vhdl:98-101](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L98-L101)：

```vhdl
function registered(signal Clock : std_logic; constant IsRegistered : boolean) return boolean is
begin
  return ite(IsRegistered, rising_edge(Clock), TRUE);
end function;
```

它返回的是一个**布尔条件**，专门配合 VHDL 的 `... when <condition>` 赋值语法使用。`IsRegistered=true` 时返回 `rising_edge(Clock)`，于是赋值变成时钟进程（插入一拍寄存器）；`IsRegistered=false` 时返回 `TRUE`，于是赋值变成无条件（等价于一根导线，组合直通）。真实用例见排序网络里 `... when registered(Clock, INSERT_PIPELINE_REGISTER);`（[sortnet_OddEvenMergeSort.vhdl:153](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/sort/sortnet/sortnet_OddEvenMergeSort.vhdl#L153)）——一个 generic 就能开关整条流水线的寄存器级数，非常优雅。

如果想看更多组合原语，可以顺手浏览：

- **计数器**：`upcounter_next`/`upcounter_equal` 与 `downcounter_next`/`downcounter_equal`/`downcounter_neg`，见 [components.vhdl:181-219](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L181-L219)。和触发器一样，`*_next` 返回下一计数值，调用方负责在时钟进程里写回。
- **移位/旋转寄存器**：`shreg_left`/`shreg_right`/`rreg_left`/`rreg_right`，见 [components.vhdl:223-241](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L223-L241)。它们内部都调用了 `mux` 来实现「`en=0` 时保持、`en=1` 时移位」。
- **比较器**：`comp`/`comp_allzero`/`comp_allone`，见 [components.vhdl:249-305](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L249-L305)。`comp` 返回一个 2 位编码（`"10"` 表示小于、`"00"` 表示相等、`"01"` 表示大于）。

#### 4.2.4 代码实践

**实践目标**：用 `mux` 写一个二选一向量选择器，对比「函数调用」与「手写条件赋值」两种写法。

**操作步骤**：

1. 在你的沙盒文件里写两段等价代码：

   ```vhdl
   -- 写法 A：用 components.mux
   Y <= mux(sel, A, B);

   -- 写法 B：传统条件赋值
   Y <= A when sel = '0' else B;
   ```

   其中 `A`、`B`、`Y` 都是 `std_logic_vector(7 downto 0)`，`sel` 是 `std_logic`。

2. 分别综合这两段代码，对比资源占用和 RTL 原理图。

**需要观察的现象**：两种写法综合出来的应是同一个二选一多路选择器（如 Xilinx 的 `MUXCY`/LUT 资源），逻辑等价。

**预期结果**：综合工具给出相同的资源报告，证明 `mux` 函数只是「更短、更统一」的写法，不引入额外开销。若暂无综合环境，可降级为「代入 `sel='0'` 与 `sel='1'` 手算 `mux` 返回值」：前者返回 `slv0`，后者返回 `slv1`。**待本地验证**实际资源对比。

#### 4.2.5 小练习与答案

**练习 1**：用 `mux` 实现一个「四选一」选择器需要几层嵌套？写出调用骨架。

**答案**：需要两层 `mux` 嵌套，用两位选择信号 `sel(1 downto 0)`：

```vhdl
Y <= mux(sel(1), mux(sel(0), in0, in1), mux(sel(0), in2, in3));
```

内层两个 `mux` 先按 `sel(0)` 各自二选一，外层再用 `sel(1)` 在两组结果之间选。

**练习 2**：`registered(Clock, false)` 在 `when` 条件里返回什么？整条赋值语句会变成什么硬件？

**答案**：根据 [components.vhdl:98-101](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L98-L101)，`registered` 返回 `ite(false, rising_edge(Clock), TRUE)` 即 `TRUE`。于是 `Y <= X when registered(Clock, false);` 退化为无条件赋值 `Y <= X;`，等价于一根组合导线（wire），不消耗任何触发器。

---

### 4.3 INIT 语义：复位值而非上电初值

#### 4.3.1 概念说明

这是本讲最重要、也最容易踩坑的一节，源头就是文件头那段 `ATTENSION`（[components.vhdl:14-17](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L14-L17)）。

很多初学者看到 `ffdre` 有个 `INIT` 参数，会理所当然地以为：「这是 FPGA 上电后触发器的初始值」。**这是错的。** 在 `components` 里：

| 名称 | 含义 | 何时生效 | 在哪里设置 |
| --- | --- | --- | --- |
| `INIT` 参数 | **复位值** | `rst='1'` 的那个时钟沿 | 函数调用参数 |
| 信号声明的初值 | **上电 / 编程初值** | 器件配置完成（上电）瞬间 | `signal q : std_logic := '0';` |

换句话说，FPGA 真正的「上电后 Q 是多少」是由**信号声明里的 `:= 初值`** 决定的（综合时会被翻译成触发器原语的 `INIT` 属性，例如 Xilinx 的 `FDCE` 的 `INIT` 参数）；而 `components` 函数里的 `INIT` 只决定「同步复位时把 Q 拉到哪个值」。两者**名字撞车、语义不同**，必须分开。

为什么会有这种「撞车」？因为 `components` 选择把复位值做成函数参数（方便用同一个 `ffdre` 既描述复位到 0、也描述复位到 1），而 VHDL 信号的上电初值只能写在声明里、无法作为运行时参数传递。所以作者特意在文件头大声提醒：**上电初值不要找 `INIT`，要回到信号声明里去设。**

#### 4.3.2 核心流程

判断「我到底需不需要管这两者的区别」可以走下面这个流程：

```text
              你写了一个 ffdre 调用
                       │
            ┌──────────▼──────────┐
            │  rst 会不会被拉高?  │
            └──────────┬──────────┘
                  是 ──┼── 否
            ┌─────────┘   └──────────┐
            ▼                          ▼
  INIT 参数 = 复位后 Q 的值     INIT 参数根本不生效
  (仍要在信号声明里写上电值)    上电值完全由信号声明决定
            │                          │
            └──────────┬───────────────┘
                       ▼
        永远要在 signal 声明里写 := 初值
        （这才是 FPGA 上电后的真实初值）
```

关键结论只有一句：**无论 `INIT` 参数填什么，信号声明里的 `:= 初值` 都不能省**——尤其在复位信号不一定每次上电都被完整拉高的设计里。

#### 4.3.3 源码精读

回到 `ffdre` 函数体（[components.vhdl:106-122](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L106-L122)），现在可以专门盯着 `INIT` 看它在两条路径里到底干了什么：

- **综合路径（`not SIMULATION`）**：`INIT` 只影响复位那一步的布尔形状——`INIT='0'` 用 `and not rst`（清零），`INIT='1'` 用 `or rst`（置一）。如果 `rst` 永远是 `'0'`，两条 `if` 分支的返回值其实一样（都退化成 `(d and en) or (q and not en)`），`INIT` 完全不参与。可见 `INIT` 在硬件里**只对应复位行为**，与上电无关。
- **仿真路径（`SIMULATION = true`）**：`elsif (rst = '1') then return INIT;`——仿真器把 `INIT` 当成「复位时直接装入的值」来建模，同样只发生在 `rst='1'` 时。仿真器里信号**一开始**的值仍由信号声明的 `:= 初值` 决定。

再看真实工程里「正确写法」的样子。排序网络核里：

```vhdl
signal Switch_r : std_logic := '0';   -- sortnet_OddEvenMergeSort.vhdl:170
...
Switch_r <= ffdre(q => Switch_r, d => Switch_d, en => Switch_en) when rising_edge(Clock);
```

这里**两件事都做了**：信号声明里 `:= '0'` 给了上电初值；`ffdre` 没传 `INIT` 所以默认 `'0'`，给了复位值。两者一致，无论上电后复位有没有被拉高，`Switch_r` 都从 0 开始——这就是稳妥的工业写法。

反过来，下面这段是**典型的错误理解**（**示例代码**，仓库中不存在）：

```vhdl
signal bad : std_logic;                 -- 忘了写 := 初值
...
bad <= ffdre(q => bad, d => d, rst => rst, INIT => '1') when rising_edge(Clock);
-- 作者以为：上电后 bad 会是 1。实际：上电值未指定（仿真为 'U'，综合依工具而定）。
```

作者传了 `INIT => '1'`，本意是「我希望它复位到 1」，这没问题；但他误以为这同时设置了上电值，于是省掉了信号初值。结果在仿真里 `bad` 一开始是 `'U'`（未初始化），直到第一次 `rst='1'` 才变成 `'1'`；在 FPGA 上电后，`bad` 的初值由综合工具默认决定（多数工具默认 0，于是和「期望的 1」相反）。正确做法是补上：

```vhdl
signal bad : std_logic := '1';          -- 上电初值，与复位值保持一致
```

#### 4.3.4 代码实践

**实践目标**：亲眼看到「`INIT` 参数只管复位、信号初值才管上电」这件事。

**操作步骤**：

1. 准备两个核（**示例代码**），唯一区别是信号声明有没有写初值：

   ```vhdl
   -- 版本 A：信号带初值
   architecture rtl of ffA is
     signal q : std_logic := '1';
   begin
     q <= ffdre(q => q, d => d, rst => rst, INIT => '1') when rising_edge(Clock);
     Qout <= q;
   end architecture;

   -- 版本 B：信号不带初值（错误示范）
   architecture rtl of ffB is
     signal q : std_logic;
   begin
     q <= ffdre(q => q, d => d, rst => rst, INIT => '1') when rising_edge(Clock);
     Qout <= q;
   end architecture;
   ```

2. 写一个测试台：**让 `rst` 在仿真开始的一段时间内保持 0**（即上电后不立刻复位），给一个固定 `d`，观察 `Qout` 从仿真的第 0 个周期起的值。

**需要观察的现象**：

- 版本 A：仿真一启动 `Qout` 就是 `'1'`（信号初值生效）。
- 版本 B：仿真启动时 `Qout` 是 `'U'`，要等到你后来把 `rst` 拉高再放开，才会变成 `'1'`（证明 `INIT` 只在 `rst='1'` 时才起作用）。

**预期结果**：两版本在 `rst` 被首次拉高之后行为一致（都是 1），但在「上电后、首次复位前」这段窗口里行为不同——版本 A 是 1，版本 B 是 `'U'`。这正是 `INIT` 参数「不是上电值」的直观证据。若没有仿真器，可降级为阅读 [components.vhdl:106-122](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L106-L122) 中 `elsif (rst = '1') then return INIT;` 这一行，确认 `INIT` 只在 `rst='1'` 分支返回。**待本地验证**仿真波形。

#### 4.3.5 小练习与答案

**练习 1**：某同事写了 `q <= ffdre(q => q, d => d) when rising_edge(Clock);`（没有 `rst`，没有 `INIT`），却抱怨「综合后上电值不确定」。他的问题出在哪？

**答案**：他既没传 `rst`/`INIT`（所以函数里根本没有任何复位相关逻辑），也没在信号声明里写 `:= 初值`。上电初值只能由信号声明决定，应该补成 `signal q : std_logic := '0';`（或想要的值）。`INIT` 参数在这种情况下完全不参与。

**练习 2**：如果希望一个寄存器「复位到 0、但上电后是 1」，应该怎么写？

**答案**：两者要分开设置——`signal q : std_logic := '1';`（上电为 1）配合 `q <= ffdre(q => q, d => d, rst => rst, INIT => '0') when rising_edge(Clock);`（复位为 0）。注意这种「上电值 ≠ 复位值」的组合是合法的，但务必确认这是你想要的行为，因为它容易让人混淆。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「**带可选寄存器的二选一选择器**」小核。它综合了 `mux`、`ffdre` 与 `registered` 三个原语，并考验你对 `INIT` 语义的理解。

**任务描述**：实现一个核 `echo_selectreg`，输入两路数据 `a`/`b`（`std_logic_vector(7 downto 0)`）、一个选择信号 `sel`、一个使能 `en`、一个复位 `rst` 和一个时钟 `Clock`；用一个 generic `REGISTER_OUTPUT : boolean` 决定输出是否寄存一拍。输出 `y` 的逻辑是：

- 先用 `mux(sel, a, b)` 选出当前数据。
- 若 `REGISTER_OUTPUT` 为真，再用 `ffdre` 把结果寄存一拍（带 `en`、`rst`，复位值 `'0'`）；若为假，直接输出。

**建议骨架（示例代码）**：

```vhdl
library IEEE;
use     IEEE.STD_LOGIC_1164.ALL;

library PoC;
use     PoC.components.all;

entity echo_selectreg is
  generic (REGISTER_OUTPUT : boolean := true);
  port    (Clock : in  std_logic;
           rst   : in  std_logic;
           en    : in  std_logic;
           sel   : in  std_logic;
           a, b  : in  std_logic_vector(7 downto 0);
           y     : out std_logic_vector(7 downto 0));
end entity;

architecture rtl of echo_selectreg is
  signal sel_d : std_logic_vector(7 downto 0);        -- mux 选出的组合结果
  signal q     : std_logic_vector(7 downto 0) := (others => '0');  -- 上电初值
begin
  sel_d <= mux(sel, a, b);

  -- 用 registered 把「是否寄存」做成可配置
  q <= ffdre(q => q, d => sel_d, rst => rst, en => en) when registered(Clock, REGISTER_OUTPUT);

  -- REGISTER_OUTPUT=false 时直接把组合结果送出
  y <= q when REGISTER_OUTPUT else sel_d;
end architecture;
```

**完成后请自查**：

1. 把 `REGISTER_OUTPUT` 设成 `true` 和 `false` 各综合一次，对比触发器数量（`true` 应多出 8 个 D 触发器）。
2. 解释为什么 `signal q` 必须写 `:= (others => '0')`，以及如果删掉它、又在仿真里不立刻拉高 `rst` 会发生什么。
3. 指出 `ffdre` 向量版（[components.vhdl:124-132](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/components.vhdl#L124-L132)）是如何逐位复用标量 `ffdre` 的，并说明 `INIT` 默认 `(0 to 0 => '0')` 经 `resize` 后的含义。

如果你能在自查里答清楚第 2 点（上电初值 vs 复位值），本讲的核心就拿下了。

## 6. 本讲小结

- `components` 包把触发器、选择器、计数器、移位寄存器、比较器等原语**写成函数**，调用方一行就能用；但它**不在 `context Common` 套餐里**，使用前必须单独 `use PoC.components.all;`。
- 触发器函数（如 `ffdre`）只返回「下一态」组合逻辑，真正的存储**靠调用方的 `... when rising_edge(Clock)` 自反馈**长出来；函数自己没有时钟、没有状态。
- `ffdre` 用 `SIMULATION` 在「仿真模型（`rst='1'` 直接返回 `INIT`）」和「可综合布尔式（`and not rst` / `or rst`）」之间切换；`ffdse`/`fftse` 通过调用 `ffdre`/`fftre` 并置 `INIT='1'` 实现复用。
- `mux` 是位宽无关的二选一选择器（标量/向量/`unsigned`/`signed` 四重载），`en` 本质就是内嵌的 `mux`；`registered` 把「要不要寄存一拍」做成可配置的 `when` 条件。
- **最关键的坑**：`INIT` 参数是**复位值**，不是 FPGA 上电初值；上电初值必须写在信号声明 `signal q := ...` 里，两者名字撞车但语义不同，工程上稳妥做法是两个都设且保持一致。

## 7. 下一步学习建议

- 接下来可以进入 [u3-l1（命名空间包模式）](u3-l1-namespace-package-pattern.md)，看具体某个命名空间的 `<ns>.pkg.vhdl` 是如何集中声明组件、并把本讲的 `components` 原语组合成完整 IP 核的。
- 想看真实工程如何大量使用本讲原语，推荐直接读排序网络 `src/sort/sortnet/sortnet_OddEvenMergeSort.vhdl`，它把 `ffdre` + `mux` + `registered` 三者用在了同一个比较-交换单元里。
- 如果你关心可综合触发器在跨厂商时的差异，可以在学完 [u3-l2（厂商选择与可移植机制）](u3-l2-vendor-selection-portability.md) 后回头思考：`components` 的 `ffdre` 是纯 RTL 布尔式，为什么它「天然跨厂商」，而 `ddrio_*`、`ocram_*` 却需要厂商专用子实体。

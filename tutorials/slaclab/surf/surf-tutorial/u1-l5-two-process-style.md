# 双进程 RTL 风格：RegType / comb / seq

## 1. 本讲目标

u1-l4 讲到了一件事：一个 SURF 模块的**外壳**（端口、`TPD_G`/`RST_POLARITY_G`/`RST_ASYNC_G` 三个约定泛型、命名后缀、`RegType`/`REG_INIT_C` 的声明）应该长什么样。但外壳只是骨架，真正"做事情"的电路还没有写——那一半的活，几乎全都落在一种固定的写法上：**双进程风格（two-process style）**。

SURF 全仓库的时序逻辑（状态机、计数器、握手、FIFO 指针、协议核的寄存器块……）几乎都用同一种套路来写：一个叫 `comb` 的组合进程算"下一拍的状态"，一个叫 `seq` 的时序进程把它打进寄存器。这套写法由 Gaisler 推广，SURF 在 [AGENTS.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md) 里把它写成硬约定。

学完本讲，你应当能够：

- 看到任何一个 SURF 模块，一眼认出"`comb` 算次态、`seq` 打寄存器"的结构，并说出 `r`、`rin`、`v` 三者各扮演什么角色。
- 读懂 `comb` 里"`v := r;` → 改 `v` → `rin <= v;` → 从 `r` 驱动输出"这条标准流水线，并理解为什么输出要从 `r`（而不是 `v`）取。
- 读懂 `seq` 里"先异步复位、再上升沿 `r <= rin after TPD_G`"的固定模板，并说清 `RST_ASYNC_G` 在 `true`/`false` 时分别走哪条路径。
- 以 `Arbiter.vhd` 为模板，自己仿写一个最小的双进程模块。

> 本讲是 u1-l4 的直接续篇。u1-l4 已经讲清了三个复位泛型的**语义**和命名约定；本讲不再重复，而是讲它们在 `comb`/`seq` **内部**到底怎么被使用。

## 2. 前置知识

进入源码前，先把几个 VHDL 概念用大白话过一遍（本讲会反复用到）：

- **信号（signal）与变量（variable）**：在 VHDL 里，`signal` 的赋值（`<=`）要到进程结束（或下一个 delta 周期）才生效，有"延迟一拍"的味道；`variable` 的赋值（`:=`）在进程里**立刻**生效。双进程风格的关键技巧就是：在 `comb` 里用一个 `variable v` 立刻反映"下一拍应该是什么样"，算完再把它整体赋给次态信号 `rin`。
- **组合逻辑（combinational）**：输出只取决于当前输入、没有记忆的电路。`comb` 进程描述的就是组合逻辑——它纯函数式地把"现态 `r` + 输入"算成"次态 `rin`"。
- **时序逻辑（sequential）**：有时钟、有记忆的电路。`seq` 进程只在时钟上升沿更新寄存器。
- **进程的敏感表（sensitivity list）**：写在 `process(...)` 括号里的信号列表。组合进程必须把它读到的所有信号都列进去，否则仿真和综合会对不上。SURF 大多用**显式列表**而不是 VHDL-2008 的 `process(all)`，为的是和大量老代码保持一致。
- **复位（reset）**：复习一下 u1-l4：`RST_ASYNC_G = false` 时复位是**同步**的（要等时钟沿，在 `comb` 里处理）；`RST_ASYNC_G = true` 时复位是**异步**的（不等时钟，在 `seq` 里处理）。本讲会看到这两条路径在代码里具体长什么样。

如果你对"信号赋值要延迟、变量赋值立刻生效"这点还半信半疑，没关系，第 4.2 节会用真实代码把它讲透——这正是双进程风格能成立的物理基础。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用来讲什么 |
|------|------|----------------|
| [base/general/rtl/Arbiter.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd) | "教科书级"小模块：固定优先级 + 轮询的仲裁器 | 作为最干净、最短的真实范例，逐行拆 `RegType`→`comb`→`seq` 全流程 |
| [base/general/rtl/Debouncer.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Debouncer.vhd) | 按键去抖器：含计数器、边沿检测、可选同步器 | 展示同一个套路如何承载更复杂的状态（计数器 + 多分支） |
| [AGENTS.md](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md) | 贡献者宪法 | 引用 "Two-Process VHDL Style" 一节，作为本讲每条规则的权威出处 |
| [base/general/rtl/StdRtlPkg.vhd](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/StdRtlPkg.vhd) | 地基包（u1-l4 主角） | 只引用 `bitSize`/`ite` 两个工具函数，说明 `comb` 里常见的位宽/分支计算 |

`Arbiter.vhd` 全文只有 90 行，却完整包含了"声明记录 → 写 `comb` → 写 `seq`"三件套，是初学者最好的模板。本讲主线就沿着它走，`Debouncer.vhd` 作为"更复杂一点"的对照。

## 4. 核心概念与源码讲解

### 4.1 把状态打包成记录：RegType / REG_INIT_C

#### 4.1.1 概念说明

一个时序电路"记住的东西"就是它的**状态**。计数器记住当前计数值，状态机记住当前在哪个状态，仲裁器记住"上一轮选了谁"。SURF 的约定是：把同一个寄存器组/同一个时钟域的所有状态字段，**打包成一个 `record`**，起名叫 `RegType`。

为什么要打包？因为时序逻辑天然有"现态"和"次态"两份：

- **现态 `r`**：当前这一拍寄存器里实际存着的值。
- **次态 `rin`**：算出来下一拍要存进去的值。

如果状态有 5 个字段，你就得维护 5 个现态信号 + 5 个次态信号，赋值时还得逐个搬——既啰嗦又容易漏。把它们装进一个记录后，"整组搬一次"变成一行：`rin <= v;` 或 `r <= rin;`。这就是双进程风格里大量用记录的根本原因。

此外，复位时所有寄存器要回到已知值。SURF 用一个常量 `REG_INIT_C`（注意 `_INIT_C` 后缀，u1-l4 讲过）集中描述这个"复位态/上电态"，需要复位时直接 `v := REG_INIT_C;`（同步）或 `r <= REG_INIT_C after TPD_G;`（异步）整体赋值，干净利落。

#### 4.1.2 核心流程

声明状态的标准三步：

1. 定义 `type RegType is record ... end record;`，把本模块所有需要寄存的字段列出来。
2. 定义 `constant REG_INIT_C : RegType := (...);`，给每个字段一个复位/上电初值（常用 `(others => '0')`）。
3. 声明两个信号：`signal r : RegType := REG_INIT_C;`（现态，带初值用于上电）和 `signal rin : RegType;`（次态，无需初值，因为每个时钟沿都会被 `comb` 重算）。

> 小细节：`r` 的声明里带 `:= REG_INIT_C`，是为了**上电那一刻**（复位还没来之前）寄存器也是确定值；`rin` 不带初值，因为它纯粹是 `comb` 的组合输出。

#### 4.1.3 源码精读

先看 `Arbiter.vhd` 的状态声明。仲裁器要记住两件事：上一轮选中的请求编号 `lastSelected`、以及当前是否有效 `valid` 和对应的一拍 `ack`：

[base/general/rtl/Arbiter.vhd:44-56](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L44-L56) —— 定义 `RegType` 记录（`lastSelected`/`valid`/`ack` 三个字段）、`REG_INIT_C` 复位常量、以及现态 `r` 与次态 `rin` 两个信号：

```vhdl
   type RegType is record
      lastSelected : slv(SELECTED_SIZE_C-1 downto 0);
      valid        : sl;
      ack          : slv(REQ_SIZE_G-1 downto 0);
   end record RegType;

   constant REG_INIT_C : RegType := (
      lastSelected => (others => '0'),
      valid        => '0',
      ack          => (others => '0'));

   signal r   : RegType := REG_INIT_C;
   signal rin : RegType;
```

注意几个点：

- `SELECTED_SIZE_C` 是个 `_C` 常量（[Arbiter.vhd:42](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L42)），用 u1-l4 讲过的 `bitSize(REQ_SIZE_G-1)` 算出"表示请求编号需要几bit"——典型的"用工具函数而非硬编码位宽"。
- `REG_INIT_C` 里向量字段用 `(others => '0')` 一次性清零，标量字段直接给 `'0'`。
- `ack` 字段虽然是个输出，但因为它是"寄存过的输出"，所以也进了 `RegType`——这呼应了 AGENTS.md 的"把寄存输出也放进记录"。

再看一个更复杂的例子 `Debouncer.vhd`。去抖器要记一个计数器 `filter`、一拍延迟的同步输入 `iSyncedDly`、以及输出 `o`：

[base/general/rtl/Debouncer.vhd:47-59](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Debouncer.vhd#L47-L59) —— 同样的三件套，但状态更丰富（含一个有界整数计数器）：

```vhdl
   type RegType is record
      filter     : integer range 0 to CNT_MAX_C;
      iSyncedDly : sl;
      o          : sl;
   end record RegType;

   constant REG_RESET_C : RegType := (
      filter     => 0,
      iSyncedDly => not INPUT_POLARITY_G,
      o          => not OUTPUT_POLARITY_G);

   signal r   : RegType := REG_RESET_C;
   signal rin : RegType;
```

这里有个值得记住的**变体**：

- 初值常量在 `Arbiter` 里叫 `REG_INIT_C`，在 `Debouncer` 里叫 `REG_RESET_C`。AGENTS.md 没有强制规定名字必须是 `REG_INIT_C`，只要遵循"`_C` 常量 + 名字能表达'复位态'"即可。读老代码时要灵活，写新代码时跟**同文件/同目录**的惯例走（[AGENTS.md:42](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L42)）。
- `Debouncer` 的 `filter` 字段是 `integer range 0 to CNT_MAX_C`——记录里完全可以放有界整数，综合工具会按范围推断位宽。`CNT_MAX_C` 又是用 u1-l4 的 `getTimeRatio(...)` 算出来的（[Debouncer.vhd:43](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Debouncer.vhd#L43)）。

#### 4.1.4 代码实践

**实践目标**：动手声明一个最小的状态记录，体会"打包"的好处。

**操作步骤**：

1. 打开 [Arbiter.vhd:44-56](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L44-L56)，把它抄成一张草稿。
2. 假设你要写一个"脉冲转电平"模块：每来一个单周期脉冲 `pulse`，输出电平 `level` 翻转一次。它只需要寄存一个字段：当前电平 `level : sl`。
3. 写出对应的 `RegType`、`REG_INIT_C`、`r`、`rin`。

**需要观察的现象**：你会发现这个模块的状态只有一个 bit，但仍然值得放进 `RegType`——因为它要参加 `comb`/`seq` 的整体搬移（第 5 节综合实践会用到）。

**预期结果**（关键三行）：

```vhdl
   -- 示例代码（非仓库原有，本讲为讲解自造）
   type RegType is record
      level : sl;
   end record RegType;
   constant REG_INIT_C : RegType := (level => '0');
   signal r   : RegType := REG_INIT_C;
   signal rin : RegType;
```

#### 4.1.5 小练习与答案

**练习 1**：`Arbiter.vhd` 里 `ack` 是模块的输出端口，为什么它也被塞进了 `RegType` 记录？

**参考答案**：因为 `ack` 是**寄存过的输出**（每个时钟沿更新一拍授予信号），属于"需要被寄存的状态"，所以和 `lastSelected`/`valid` 一起进记录，才能在 `comb` 里被统一算成次态、在 `seq` 里被统一打一拍。组合输出（不需要寄存的）才不进记录。

**练习 2**：`r` 的声明带 `:= REG_INIT_C`，`rin` 不带。为什么？

**参考答案**：`r` 是真实寄存器，上电瞬间（复位还没到）也必须是确定值，所以给它一个初值；`rin` 只是 `comb` 的组合输出，每个时钟沿都会被重新计算并赋给 `r`，给它初值没有意义。

---

### 4.2 comb 进程：用变量算次态、从 r 驱动输出

#### 4.2.1 概念说明

`comb` 是双进程里的"大脑"。它是一个**组合进程**，职责只有一个：给定"现态 `r` + 各路输入"，算出"次态 `rin`"。

它的写法靠一个核心技巧——**变量 `v`**：

1. 进程开头声明 `variable v : RegType;`，并立刻 `v := r;`（把现态拷一份到变量里）。
2. 之后所有的次态更新都**改 `v`**，而不是改 `r`（`r` 是信号，进程里改它要到 delta 周期才生效，会乱套；改变量则立刻生效，便于后续判断依赖 `v.level` 的逻辑）。
3. 算完后，`rin <= v;` 把变量整体搬给次态信号。
4. 模块的寄存输出**从 `r`**（现态）驱动，不从 `v`。

为什么输出要从 `r` 取、不从 `v` 取？因为 `r` 才是"这一拍真正寄存着的值"，从 `r` 取输出意味着输出是**寄存输出**（相对输入延迟一拍，时序好）。从 `v` 取则是组合输出（当拍就变，路径长）。AGENTS.md 把这条写成规则：默认从 `r` 驱动，只有当设计**有意**要暴露"下一拍/组合"行为时才从 `v` 取。

为什么必须用变量、不能直接 `rin.x <= ...`？因为同一个进程里后面的判断可能要依赖前面改过的次态值。若用信号赋值，所有赋值要到进程结束才一起生效，"先改后判"就失效了；用变量则按顺序立刻生效，能正确表达"基于次态的进一步计算"。

#### 4.2.2 核心流程

`comb` 进程的标准骨架（伪代码）：

```
comb : process(现态 r, 所有读到的输入, rst) is
    variable v : RegType;
begin
    v := r;                       -- ① 把现态拷进变量

    -- ② 次态逻辑：根据输入和 r 计算下一拍，全部改 v
    if (某条件) then
        v.某字段 := 新值;
    end if;

    -- ③ 同步复位：RST_ASYNC_G=false 时在这里把 v 整体清回初值
    if (RST_ASYNC_G = false and rst = RST_POLARITY_G) then
        v := REG_INIT_C;
    end if;

    rin   <= v;                   -- ④ 把算好的次态交给 rin
    输出  <= r.某字段;             -- ⑤ 寄存输出从现态 r 取
end process comb;
```

五步的顺序很重要：**先算次态，再处理同步复位**。这样复位永远能"覆盖"任何次态计算，保证复位期间输出确定。注意异步复位**不**出现在 `comb` 里——它属于 `seq`（见 4.3）。

#### 4.2.3 源码精读

逐段看 `Arbiter.vhd` 的 `comb`：

[base/general/rtl/Arbiter.vhd:60-78](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L60-L78) —— 完整的 `comb` 进程：

```vhdl
   comb : process (r, req, rst) is
      variable v : RegType;
   begin
      v := r;

      if (req(conv_integer(r.lastSelected)) = '0' or r.valid = '0') then
         arbitrate(req, r.lastSelected, v.lastSelected, v.valid, v.ack);
      end if;

      if (RST_ASYNC_G = false and rst = RST_POLARITY_G) then
         v := REG_INIT_C;
      end if;

      rin      <= v;
      ack      <= r.ack;
      valid    <= r.valid;
      selected <= slv(r.lastSelected);

   end process comb;
```

对照五步骨架：

- **敏感表**：`(r, req, rst)`——列出了它读到的现态 `r`、输入 `req`、复位 `rst`。注意没有用 `process(all)`，这是 SURF 老代码的普遍习惯（[AGENTS.md:45](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L45)）。
- **①**：`v := r;`（第 63 行）。
- **②**：仲裁逻辑。这里把"上一个被选中的请求还在要、且当前有效"时**保持**授予不变，否则调用 `ArbiterPkg` 里的 `arbitrate` 过程重新仲裁（第 65–67 行）。注意：`arbitrate` 过程的 `nextSelected`/`valid` 是 `inout`，传进去的是 **`v`** 的字段（`v.lastSelected` 等），因为它要更新的是**次态**。这正是"改 `v` 不改 `r`"的体现。
- **③ 同步复位**（第 69–71 行）：`if (RST_ASYNC_G = false and rst = RST_POLARITY_G) then v := REG_INIT_C;`。两个条件缺一不可——`RST_ASYNC_G = false` 表示"本模块是同步复位"，`rst = RST_POLARITY_G` 表示"复位当前被 assert"。两者都满足才清零，且因为放在次态逻辑**之后**，能覆盖前面算的任何 `v`。
- **④**：`rin <= v;`（第 73 行）。
- **⑤ 寄存输出**：`ack`、`valid`、`selected` 全部从 **`r`**（现态）取（第 74–76 行），意味着这些输出相对输入延迟一拍。这正是 AGENTS.md 要求的"默认从 `r` 驱动输出"（[AGENTS.md:44](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L44)）。

再看 `Debouncer.vhd` 的 `comb`，体会"更复杂的状态机"如何套同一个骨架：

[base/general/rtl/Debouncer.vhd:101-136](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Debouncer.vhd#L101-L136) —— 去抖器的 `comb`，含计数器递减、边沿检测、极性处理：

```vhdl
   comb : process (iSynced, r, rst) is
      variable v : RegType;
   begin
      v := r;

      v.iSyncedDly := iSynced;

      if SYNCHRONIZE_G and SYNC_EDGE_TRIG_G and (iSynced = INPUT_POLARITY_G) then
         v.filter := 0;
      elsif (r.iSyncedDly /= iSynced) then  -- any edge
         v.filter := CNT_MAX_C;
      elsif (r.filter /= 0) then
         v.filter := r.filter - 1;
      end if;

      if POLARITY_EQ_C then
         if (r.filter = 0 and r.o /= r.iSyncedDly) then
            v.o := r.iSyncedDly;
         end if;
      else
         if (r.filter = 0 and r.o = r.iSyncedDly) then
            v.o := not r.iSyncedDly;
         end if;
      end if;

      -- Synchronous Reset
      if (RST_ASYNC_G = false and rst = RST_POLARITY_G) then
         v := REG_RESET_C;
      end if;

      rin <= v;
      o   <= r.o;

   end process comb;
```

观察要点：

- 第 106 行 `v.iSyncedDly := iSynced;` 是无条件更新——每个组合周期都把当前输入记进次态的延迟字段，下一拍它就成了"上一拍的输入"，供边沿检测用。
- 第 108–114 行是一串 `elsif`：边沿来了就重装计数器 `v.filter := CNT_MAX_C`，否则计数器递减 `v.filter := r.filter - 1`。这里**混用** `v.filter`（读次态）和 `r.filter`（读现态）：递减用的是 `r.filter - 1` 写进 `v.filter`。读老代码时要分清每个引用读的是 `r`（现态）还是 `v`（次态）。
- 第 116–126 行根据 `POLARITY_EQ_C`（又一个用 u1-l4 `ite` 算出的常量，[Debouncer.vhd:44](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Debouncer.vhd#L44)）决定输出何时翻转。
- **同步复位**（第 129–131 行）和 `Arbiter` 一字不差：`if (RST_ASYNC_G = false and rst = RST_POLARITY_G) then v := REG_RESET_C;`，且放在所有次态逻辑之后。
- **输出**（第 134 行）：`o <= r.o;`，同样从现态取，寄存输出。

> 一个统一的规律：无论模块多复杂，`comb` 的**开头三行**（`variable v`、`v := r`、之后开始改 `v`）和**结尾三段**（同步复位、`rin <= v`、输出从 `r` 取）几乎是固定的。变化只夹在中间的"②次态逻辑"。记住这个"三明治"结构，读任何 SURF 时序模块都快。

#### 4.2.4 代码实践

**实践目标**：通过手工"执行"一遍 `Arbiter` 的 `comb`，验证你真的看懂了 `v`/`r` 的区别。

**操作步骤**：

1. 假设 `REQ_SIZE_G = 4`，当前 `r.lastSelected = 2`、`r.valid = '1'`、`r.ack = "0000"`。
2. 给定输入 `req = "1000"`（即只有请求 3 在要）。
3. 逐步走 `comb`：因为 `req(conv_integer(r.lastSelected)) = req(2) = '0'`（请求 2 没在要），条件成立，进入 `arbitrate`。
4. 想象 `arbitrate` 把 `v.lastSelected` 改成 3、`v.valid := '1'`、`v.ack := "1000"`。
5. 没有复位。于是 `rin` 拿到的是 `v`（含 `lastSelected=3`）。

**需要观察的现象**：注意 `ack`、`valid`、`selected` 这三个输出此刻从 `r`（旧值）取，所以**这一拍**端口上看到的还是旧的 `lastSelected=2`/`ack="0000"`；要到**下一个时钟沿** `seq` 把 `rin` 打进 `r` 后，输出才变成 3。

**预期结果**：输出比次态晚一拍。这正是"从 `r` 驱动输出 = 寄存输出"的物理含义。如果本模块把输出改成从 `v` 取，输出当拍就会变——但组合路径会变长，时序变差。这就是 AGENTS.md 默认要求从 `r` 取的原因。

（本实践为源码阅读/推演型，不要求运行；如想验证，可参考第 5 节用 cocotb 给 `Arbiter` 喂激励观察输出延迟。）

#### 4.2.5 小练习与答案

**练习 1**：如果把 `Arbiter` 的 `comb` 里 `v := r;` 这一行删掉，会发生什么？

**参考答案**：`v` 成了未初始化的变量，每次进程触发它的值不确定（仿真里是 `'U'`）。所有"保持不变"的字段（例如上面 `if` 不成立时本应保持 `lastSelected`）会丢失现态值，电路行为全错。`v := r;` 的作用就是"默认继承现态"，让你只写**需要改变**的字段。

**练习 2**：`comb` 的同步复位分支为什么必须放在次态逻辑**之后**？

**参考答案**：放在之后才能"最后生效"，用 `v := REG_INIT_C;` 整体覆盖前面算出的任何次态，保证复位期间 `rin` 一定是初值、输出确定。若放在之前，后面的次态逻辑又把 `v` 改回去，复位就失效了。

**练习 3**：`Debouncer` 的 `comb` 敏感表是 `(iSynced, r, rst)`。为什么是 `iSynced` 而不是原始输入 `i`？

**参考答案**：`comb` 实际读的是经过（可选）同步器后的 `iSynced` 信号（见 [Debouncer.vhd:65-99](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Debouncer.vhd#L65-L99) 的 `generate` 块），不是直接读 `i`。组合进程的敏感表必须列出它**真正读取**的信号，否则仿真结果会和综合不一致。

---

### 4.3 seq 进程：上升沿打寄存器 + 异步复位

#### 4.3.1 概念说明

`seq` 是双进程里的"手"。它是个**时序进程**，对时钟敏感，职责极简：在时钟上升沿把次态 `rin` 打进现态寄存器 `r`。

`seq` 几乎**不做任何逻辑判断**——所有"算什么"的活都在 `comb` 干完了，`seq` 只负责"搬"。它的全部内容通常就是一个 `if/elsif`：先判异步复位，再判上升沿。

异步复位为什么放在 `seq` 而不是 `comb`？因为异步复位的定义就是"不等时钟、一旦 assert 立刻把寄存器拉回初值"，这只能由对 `rst` 也敏感的时序进程来做。同步复位则相反——它要等时钟沿才生效，所以在 `comb` 的次态路径里处理（让"复位后的次态"正好是初值，下一沿打进去）。这就是 u1-l4 说的"`RST_ASYNC_G` 决定复位走 `comb` 还是 `seq`"在代码层面的落点。

关于 `after TPD_G`：这是 VHDL 的**惯性延迟**标注，只在仿真里有意义——它让波形上的跳变延迟 `TPD_G`（默认 1 ns）发生，便于肉眼分辨多根信号的前后。综合工具会**忽略** `after` 子句，所以它不影响真实电路。SURF 约定在 `seq` 里保留它（[AGENTS.md:48](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L48)）。

#### 4.3.2 核心流程

`seq` 进程的固定模板（伪代码）：

```
seq : process(clk, rst) is
begin
    if (RST_ASYNC_G and rst = RST_POLARITY_G) then
        r <= REG_INIT_C after TPD_G;        -- ① 异步复位：最高优先级，不等时钟
    elsif rising_edge(clk) then
        r <= rin after TPD_G;               -- ② 上升沿：把 comb 算好的次态打进现态
    end if;
end process seq;
```

两个分支的顺序是铁律：**异步复位在前、上升沿在后**。这样复位一旦 assert，无论时钟在不在跳，都立刻把 `r` 拉回初值。

需要特别理解的，是 `RST_ASYNC_G` 这一个泛型如何**同时**影响 `comb` 和 `seq` 两条路径：

- `RST_ASYNC_G = false`（同步复位）：
  - `comb` 里的同步复位分支**生效**（`if (RST_ASYNC_G = false and ...) then v := REG_INIT_C;`）。
  - `seq` 里的异步复位分支**不生效**（`if (RST_ASYNC_G and ...)` 中 `RST_ASYNC_G` 为 `false`，整个 if 为假）。
- `RST_ASYNC_G = true`（异步复位）：
  - `comb` 里的同步复位分支**不生效**（`RST_ASYNC_G = false` 为假）。
  - `seq` 里的异步复位分支**生效**。

两条路径靠同一个泛型互斥地切换，复位永远只会从一边走，不会重复。这是一个非常优雅的设计——同一个模块源码，靠一个泛型就能在"同步复位"和"异步复位"之间切换，而不用改任何逻辑。

#### 4.3.3 源码精读

看 `Arbiter.vhd` 的 `seq`，它和上面的模板几乎一字不差：

[base/general/rtl/Arbiter.vhd:80-87](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L80-L87) —— 完整的 `seq` 进程：

```vhdl
   seq : process (clk, rst) is
   begin
      if (RST_ASYNC_G and rst = RST_POLARITY_G) then
         r <= REG_INIT_C after TPD_G;
      elsif rising_edge(clk) then
         r <= rin after TPD_G;
      end if;
   end process seq;
```

逐点说明：

- **敏感表 `(clk, rst)`**：既对时钟敏感、也对复位敏感。异步复位必须把 `rst` 放进敏感表，否则复位沿到来时进程不触发，寄存器不会立刻复位。
- **① 异步复位**：`if (RST_ASYNC_G and rst = RST_POLARITY_G) then r <= REG_INIT_C after TPD_G;`。两个条件：本模块确实开了异步复位（`RST_ASYNC_G` 为 `true`），且复位当前被 assert（`rst = RST_POLARITY_G`，与极性解耦——u1-l4 讲过）。
- **② 上升沿**：`elsif rising_edge(clk) then r <= rin after TPD_G;`。把 `comb` 算好的次态 `rin` 打进现态 `r`，延迟 `TPD_G` 仅影响波形。

`Debouncer.vhd` 的 `seq` 结构完全相同：

[base/general/rtl/Debouncer.vhd:138-145](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Debouncer.vhd#L138-L145) —— 同一个模板，只是初值常量名是 `REG_RESET_C`：

```vhdl
   seq : process (clk, rst) is
   begin
      if (RST_ASYNC_G and rst = RST_POLARITY_G) then
         r <= REG_RESET_C after TPD_G;
      elsif (rising_edge(clk)) then
         r <= rin after TPD_G;
      end if;
   end process seq;
```

> 你会发现全仓库的 `seq` 进程都长这副模样。读代码时，`seq` 基本可以"扫一眼跳过"——真正的逻辑全在 `comb`。这是双进程风格的一大阅读红利：**把"算什么"和"何时落寄存器"彻底解耦**，看 `comb` 就懂行为，看 `seq` 只为确认复位/时钟约定。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `RST_ASYNC_G` 在 `true`/`false` 时，复位分别走哪条路径。

**操作步骤**：

1. 打开 [Arbiter.vhd:60-87](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L60-L87)，把 `comb`（第 60–78 行）和 `seq`（第 80–87 行）并排放。
2. **情形 A**：假设 `RST_ASYNC_G = false`（同步复位）。回答：复位 assert 时，`comb` 第 69 行的 `if` 成立吗？`seq` 第 82 行的 `if` 成立吗？`r` 在哪个时刻、由谁更新成 `REG_INIT_C`？
3. **情形 B**：假设 `RST_ASYNC_G = true`（异步复位）。同样回答这两个 `if` 各自成立吗？这次 `r` 由谁、在什么条件下更新成 `REG_INIT_C`？

**需要观察的现象**：两种情形下，**有且只有一条**路径会把 `r` 复位成初值，另一条的复位分支永远是假。

**预期结果**：

- 情形 A（同步复位）：`comb` 第 69 行 `if (RST_ASYNC_G = false and ...)` 成立 → `v := REG_INIT_C` → `rin <= v` → 要等到**下一个时钟上升沿**，`seq` 的 `elsif rising_edge(clk)` 才把 `rin`（初值）打进 `r`。`seq` 第 82 行的 `if (RST_ASYNC_G and ...)` 因 `RST_ASYNC_G` 为 `false` 而**不成立**。结论：复位要等时钟沿才生效。
- 情形 B（异步复位）：`comb` 第 69 行因 `RST_ASYNC_G = false` 为假而**不成立**；`seq` 第 82 行 `if (RST_ASYNC_G and rst = RST_POLARITY_G)` 成立 → `r <= REG_INIT_C after TPD_G`，**不等时钟**，`rst` 一 assert 就立刻把 `r` 拉回初值。

（本实践为源码阅读/推演型；如要跑波形验证，见第 5 节的 cocotb 路线。）

#### 4.3.5 小练习与答案

**练习 1**：`seq` 的敏感表为什么必须包含 `rst`，即便模块用的是同步复位（`RST_ASYNC_G = false`）？

**参考答案**：因为同一份源码要能切换成异步复位。当 `RST_ASYNC_G = true` 时，`seq` 必须对 `rst` 敏感才能在复位沿立刻触发并复位寄存器。即便当前实例是同步复位、`rst` 在敏感表里也不会有副作用（对应的 `if` 分支恒假），所以统一写上 `rst` 是安全的、也是 SURF 的统一约定。

**练习 2**：`after TPD_G` 综合后会变成真实延迟吗？为什么 SURF 还要保留它？

**参考答案**：不会。综合工具忽略 `after` 子句，它只在仿真里让信号跳变延迟 `TPD_G`（默认 1 ns）发生，便于在波形里分辨多根信号的前后顺序、避免 delta 周期堆叠导致的"看起来同时跳"。SURF 约定保留它（[AGENTS.md:48](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L48)），纯粹是为了仿真可读性。

**练习 3**：为什么 `seq` 里几乎没有逻辑判断、只有"复位 / 上升沿"两个分支？

**参考答案**：因为双进程风格把"算次态"完全交给 `comb`，`seq` 只负责在时钟沿把 `comb` 算好的 `rin` 原样搬进 `r`。这样"算什么"和"何时落寄存器"解耦：读 `comb` 就能理解全部行为，`seq` 只用于确认时钟/复位约定。逻辑塞进 `seq` 反而会破坏这个清晰的分工。

---

## 5. 综合实践：仿写一个"脉冲转电平"双进程模块

把本讲三块内容（`RegType`/`comb`/`seq`）串起来，自己写一个最小但**完整**的模块：**`PulseToLevel`**——输入一个单比特、单周期的 `pulse`，每来一个脉冲就把输出电平 `level` 翻转一次（本质是一个 T 触发器）。它需要寄存一个状态（当前电平），完美适合双进程风格，而且短到可以一眼看全。

### 5.1 实践目标

- 把 `Arbiter.vhd` 当模板，独立写出一个含 `RegType`/`REG_INIT_C`/`r`/`rin`/`comb`/`seq` 的完整实体。
- 保留 `TPD_G` 与 `RST_ASYNC_G` 的标准行为（同步/异步复位两条路径都能切换）。
- 用 u1-l2 讲过的 GHDL 语法分析（或第 9 单元的 cocotb）验证它至少能编译通过、行为正确。

### 5.2 参考实现（示例代码）

下面这份是**本讲为讲解自造的示例代码**，不是仓库原有文件。它的每一处写法都严格对照 `Arbiter.vhd`：

```vhdl
-- 示例代码（非仓库原有，本讲为讲解自造）
-- 文件名建议：PulseToLevel.vhd
library ieee;
use ieee.std_logic_1164.all;

library surf;
use surf.StdRtlPkg.all;

entity PulseToLevel is
   generic (
      TPD_G          : time    := 1 ns;   -- 与 Arbiter.vhd:26 一致
      RST_POLARITY_G : sl      := '1';    -- 与 Arbiter.vhd:27 一致
      RST_ASYNC_G    : boolean := false); -- 与 Arbiter.vhd:28 一致
   port (
      clk   : in  sl;
      rst   : in  sl := not RST_POLARITY_G;  -- 与 Arbiter.vhd:32 一致：可选复位
      pulse : in  sl;                         -- 单比特、单周期脉冲
      level : out sl);                        -- 每来一个脉冲翻转一次的电平
end entity PulseToLevel;

architecture rtl of PulseToLevel is

   -- ① RegType / REG_INIT_C（对应 4.1 节）
   type RegType is record
      level : sl;
   end record RegType;

   constant REG_INIT_C : RegType := (
      level => '0');

   signal r   : RegType := REG_INIT_C;
   signal rin : RegType;

begin

   -- ② comb：用变量 v 算次态，输出从 r 取（对应 4.2 节）
   comb : process (r, pulse, rst) is
      variable v : RegType;
   begin
      v := r;

      if (pulse = '1') then
         v.level := not r.level;   -- 来一个脉冲就翻转次态电平
      end if;

      -- 同步复位
      if (RST_ASYNC_G = false and rst = RST_POLARITY_G) then
         v := REG_INIT_C;
      end if;

      rin   <= v;
      level <= r.level;            -- 寄存输出，从现态 r 取
   end process comb;

   -- ③ seq：上升沿打寄存器 + 异步复位（对应 4.3 节）
   seq : process (clk, rst) is
   begin
      if (RST_ASYNC_G and rst = RST_POLARITY_G) then
         r <= REG_INIT_C after TPD_G;
      elsif rising_edge(clk) then
         r <= rin after TPD_G;
      end if;
   end process seq;

end architecture rtl;
```

### 5.3 操作步骤

1. **对照模板**：把上面这份代码和 [Arbiter.vhd:40-89](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L40-L89) 逐段比对，确认三处一一对应：①记录声明 → ②`comb` → ③`seq`。
2. **手动推演行为**：假设 `level` 初值为 `'0'`，连续给 3 个相隔若干拍的 `pulse='1'`。手算每次脉冲后 `r.level` 的值（应依次为 `1 → 0 → 1`）。
3. **验证复位双路径**：
   - 置 `RST_ASYNC_G = false`：让 `rst='1'` 期间保持给 `pulse`，确认输出在**下一个时钟沿**才回到 `'0'`（同步）。
   - 置 `RST_ASYNC_G = true`：让 `rst='1'`，确认输出**不等时钟**立刻回到 `'0'`（异步）。
4. **（可选）编译验证**：参照 u1-l2 的 GHDL 分析流程。由于本文件 `use surf.StdRtlPkg.all;`，需要 `surf` 库已被分析过——最省事的做法是把文件放进一个临时子目录并写一个最小 `ruckus.tcl`，再用 `make MODULES=$PWD analysis`；或直接用 `ghdl -a --std=08 --ieee=synopsys -fsynopsys --work=surf` 在已建好 `surf` 库的环境里分析。具体能否在你的机器上一次跑通，**待本地验证**（依赖 u1-l2 的 `import` 是否已建好 `surf-libs-src` 缓存）。

### 5.4 需要观察的现象与预期结果

- **脉冲→翻转**：每个 `pulse='1'` 拍之后，`level` 在下一拍翻转一次；连发的脉冲（相邻两拍都为 `'1'`）会让 `level` 每拍翻转。
- **输出延迟**：`level` 相对 `pulse` 延迟一拍——因为它是从 `r`（现态）驱动的寄存输出。这正是 4.2 节"从 `r` 取"的含义。
- **复位**：`RST_ASYNC_G` 在 `true`/`false` 之间切换时，复位分别走 `seq` / `comb`，与 4.3 节推演一致。

> 如果手头已配好 u9 单元的 cocotb 栈，可以仿照 `tests/base/` 下的测试结构给 `PulseToLevel` 写一个最小 `@cocotb.test`：复位后发若干脉冲，断言 `level` 序列。这是把本讲知识变成可执行验证的最直接路径（具体写法见 u9-l2）。

## 6. 本讲小结

- 双进程风格把时序逻辑拆成两个固定进程：`comb` 算次态、`seq` 打寄存器（[AGENTS.md:37-50](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L37-L50)）。
- 状态打包成 `type RegType is record`，用 `REG_INIT_C`（或 `REG_RESET_C`）描述复位/上电态，再声明现态 `r`（带初值）和次态 `rin`（[Arbiter.vhd:44-56](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L44-L56)）。
- `comb` 的"三明治"结构：开头 `variable v : RegType; v := r;` → 中间改 `v` → 同步复位 `if (RST_ASYNC_G = false and rst = RST_POLARITY_G) then v := REG_INIT_C;` → `rin <= v;` → 寄存输出从 `r` 取（[Arbiter.vhd:60-78](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L60-L78)）。
- 用变量 `v` 而非信号是为了"赋值立刻生效"，让后续判断能基于次态；输出默认从 `r` 取是为了得到时序更好的寄存输出。
- `seq` 几乎不含逻辑，只有"异步复位在前、上升沿 `r <= rin after TPD_G` 在后"两个分支，`after TPD_G` 仅影响仿真波形、综合被忽略（[Arbiter.vhd:80-87](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Arbiter.vhd#L80-L87)）。
- 同一个泛型 `RST_ASYNC_G` 互斥地切换 `comb`（同步复位）和 `seq`（异步复位）两条路径，复位永远只走一边——这是双进程风格里最巧妙的一处约定。

## 7. 下一步学习建议

- **进入 u2（基础库）**：本讲建立的双进程骨架，是 u2 全部内容的载体。u2-l1 的同步器（`Synchronizer`/`RstSync`）、u2-l2 的异步 FIFO（`FifoAsync` 的 Gray 指针）、u2-l5 的 `Arbiter`/`Gearbox`，全都用 `RegType`/`comb`/`seq` 写成。带着本讲的"三明治"读法去读它们，会非常顺。
- **回头精读 `Debouncer.vhd` 全文**：本讲只摘了它的 `comb`/`seq`，但它还示范了"在 `architecture` 里用 `generate` 条件实例化 `Synchronizer`/`RstSync`"（[Debouncer.vhd:65-99](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/Debouncer.vhd#L65-L99)）——这是 u2-l1 同步器讲的预备知识，值得现在通读一遍。
- **通读 `AGENTS.md` 的 "Two-Process VHDL Style" 一节**（[AGENTS.md:37-50](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L37-L50)）：本讲每条规则都出自这里，尤其 "Don't scatter registers across multiple unrelated clocked processes"（[AGENTS.md:49](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/AGENTS.md#L49)）——多时钟域时要"每域一套 `RegType`/`comb`/`seq`，并显式标注 CDC 边界"，这是 u2-l1 CDC 主题的伏笔。

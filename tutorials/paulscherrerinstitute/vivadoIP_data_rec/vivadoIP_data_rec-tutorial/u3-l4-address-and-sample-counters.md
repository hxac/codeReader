# 地址计数器、采样计数器与触发长度

## 1. 本讲目标

本讲聚焦 `data_rec` 核心记录器里两个最关键的「数数」电路：

- 环形**地址计数器** `AdrCnt`（写作 `AdrCnt_2` / `AdrCnt_3`）——决定下一个样本写进缓冲区的哪个地址；
- **采样计数器** `SplCnt`（写作 `SplCnt_2`）——决定一段录制已经采了多少个样本、后触发何时结束。

学完后你应当能够：

1. 说清 `AdrCnt` 在什么状态下复位、何时递增、何时回绕成环形；
2. 说清 `SplCnt` 为什么在等待触发时被「预置」成 `PreTrigSpls+1`，以及它如何只用一个计数器同时统计前触发与后触发样本；
3. 解释 `PreTrigSpls = 0` 时为什么能直接跳过 PreTrig 状态；
4. 给定 `MemoryDepth_g`、`PreTrigSpls`、`TotalSpls`，手算从 Arm 到 Done 全过程两个计数器的取值，并指出触发时刻它们的值。

本讲不重复状态机迁移条件（见 u3-l2）和两进程/流水线骨架（见 u3-l3），只把镜头对准「计数器本身怎么走、状态机怎么读它们」。

## 2. 前置知识

阅读本讲前，你需要先建立这几个概念（前几讲已覆盖）：

- **记录器是一个环形缓冲（ring buffer）**：存储区有 `MemoryDepth_g` 个表项，写指针走到末尾后绕回 0，永远循环。这样无论何时触发，缓冲里都保留着「最近的若干个样本」。
- **一段录制 = 前触发窗口 + 后触发窗口**：`PreTrigSpls` 个样本在触发**之前**，剩下的在触发**之后**，合计 `TotalSpls` 个样本（类似示波器抓波形）。
- **两进程法与流水级编号**（u3-l3）：信号名后的数字后缀就是它所处的流水级。本讲里 `AdrCnt_2` / `SplCnt_2` 在 Stage 2 被状态机读取与更新，`AdrCnt_3` 在 Stage 3 对齐到存储器写端口。组合进程 `p_comb` 用变量 `v` 在同一拍内顺序计算 `r_next`，模板是 `v := r;`（默认保持）→ 改字段 → `r_next <= v;`。
- **`In_Vld(1)` 是「有效样本已到达 Stage 2」的标志**：所有计数器的递增都受它门控——只有真正采到一个样本（`In_Vld=1`）才数一次，样本断流时计数器原地不动。

一个关键直觉先放在心里：**地址计数器管「写到哪里」，采样计数器管「采了多少」**。前者是空间指针（模 `MemoryDepth_g`），后者是时间长度（单调递增直到 `TotalSpls`）。两者都在 Stage 2 维护、都受 `In_Vld(1)` 门控，但用途完全不同。

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| [hdl/data_rec.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd) | 核心记录器 RTL。本讲关注其中三段：状态机 `case r.State_2`、地址计数器处理、采样计数器处理，以及 record 里两个计数器的声明与 `p_seq` 里的复位。 |

引用 `PreTrigSpls` / `TotalSpls` 的位宽定义在 entity 端口（u3-l1 已讲），寄存器地址地图在 `data_rec_register_pkg.vhd`（u3-l2 已讲），本讲只在需要时点一下，不展开。

## 4. 核心概念与源码讲解

### 4.1 环形地址计数器 AdrCnt

#### 4.1.1 概念说明

记录器不内置 RAM（RAM 在封装层，见 u5-l3），核心只输出一组「外部存储器接口」信号：写使能 `Mem_Wr`、写地址 `Mem_Adr`、写数据 `Mem_Data`。**`AdrCnt` 就是那个写地址指针**。

之所以做成「环形」，是因为在触发真正到来之前，我们不知道要丢掉多久的旧数据。做法是：进入录制后就一直把样本按 `0,1,2,…,MemoryDepth_g-1,0,1,…` 循环写入，新的覆盖最旧的。等到触发发生，缓冲里天然就保留着「触发前最近的一圈样本」，再补记后触发样本即可。这样一个固定深度的缓冲就能支撑任意长的「等待触发」时间。

`AdrCnt` 在 record 里声明成两份，对应两个流水级（[hdl/data_rec.vhd:106-107](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L106-L107)）：

```vhdl
AdrCnt_2 : unsigned(Mem_Adr'range);   -- Stage2：状态机读/更新它
AdrCnt_3 : unsigned(Mem_Adr'range);   -- Stage3：对齐到存储器写端口
```

位宽与端口 `Mem_Adr` 一致，即 \(\lceil\log_2(\text{MemoryDepth\_g})\rceil\) 位。

#### 4.1.2 核心流程

地址计数器的更新规则只有三条，按优先级排：

1. **复位条件**：状态机处于 `Idle_s` 或 `Done_s` 时，`AdrCnt_2` 强制为 0（不录制时指针归零，下次录制从地址 0 开始）。
2. **回绕条件**：正在录制（非 Idle/Done）且本拍有有效样本（`In_Vld(1)=1`），若已经指到 `MemoryDepth_g-1`，下一拍绕回 0。
3. **递增条件**：正在录制且本拍有有效样本，否则 `+1`。

注意：**没有有效样本就什么都不做**（既不递增也不回绕），所以样本断流时地址指针稳定不动，不会写脏数据。用伪代码表达：

```
if State in {Idle, Done}:
    AdrCnt_2_next = 0
elif In_Vld(1) == 1:
    if AdrCnt_2 == MemoryDepth_g - 1:
        AdrCnt_2_next = 0          # 环形回绕
    else:
        AdrCnt_2_next = AdrCnt_2 + 1
# else: 保持不变
```

#### 4.1.3 源码精读

地址计数器的全部逻辑就这三行 if-elsif（[hdl/data_rec.vhd:278-287](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L278-L287)）：

```vhdl
-- Address coutner handling
if (r.State_2 = Idle_s) or (r.State_2 = Done_s)  then
    v.AdrCnt_2	:= (others => '0');
elsif r.In_Vld(1) = '1' then
    if r.AdrCnt_2 = MemoryDepth_g-1 then
        v.AdrCnt_2 := (others => '0');
    else
        v.AdrCnt_2	:= r.AdrCnt_2+1;
    end if;
end if;
```

注意它判断的是 `r.State_2`（本拍的旧状态），而不是 `v.State_2`（本拍刚算出的新状态）。这意味着状态迁移与地址计数发生在同一拍但互不干扰——状态机刚决定「进入 PreTrig」，地址计数器看到的仍是「Idle」，于是这一拍先把指针清 0，下一拍才真正开始递增。

指针随后被搬到 Stage 3 对齐到存储器写端口（[hdl/data_rec.vhd:299](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L299)）：

```vhdl
v.AdrCnt_3 := r.AdrCnt_2;   -- 流水搬运到 Stage3
```

并最终输出（[hdl/data_rec.vhd:351](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L351)）：

```vhdl
Mem_Adr <= std_logic_vector(r.AdrCnt_3);
```

上电复位时 `p_seq` 把 `AdrCnt_2` 同步清 0（[hdl/data_rec.vhd:374](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L374)）。

#### 4.1.4 代码实践

**实践目标**：通过改参数观察环形回绕点。

1. 打开 [hdl/data_rec.vhd:278-287](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L278-L287)，确认回绕判断是 `r.AdrCnt_2 = MemoryDepth_g-1`。
2. 假设把 `MemoryDepth_g` 从 128 改成 30（非二次幂，正好对应仿真里 case0 跑的那组），手算：指针取值序列在到达哪个数后会跳回 0？
3. 对照 u1-l3 提到的仿真——`config.tcl` 会让 `top_tb` 用 `MemoryDepth_g = 32` 和 `= 30` 各跑一次，其中 30 这组正是为了覆盖这里的非二次幂回绕路径。

**需要观察的现象 / 预期结果**：

- 回绕点 = `MemoryDepth_g - 1`。`MemoryDepth_g=30` 时指针走到 29 后下一拍回 0；`MemoryDepth_g=128` 时走到 127 后回 0。
- 指针取值范围始终是 \(0 \ldots \text{MemoryDepth\_g}-1\)，永不会写出缓冲边界。

> 说明：本实践为「源码阅读 + 手算」型，未在本地执行仿真；若要实测，可按 u1-l3 在 `sim/` 下跑回归，观察波形中 `Mem_Adr` 的回绕点。

#### 4.1.5 小练习与答案

**练习 1**：为什么地址计数器在 `Idle_s` 和 `Done_s` 都要清 0，而不是只在 `Idle_s` 清？

**答案**：`Done_s` 表示一段录制刚结束、正等软件读走数据并 Ack。清 0 是为了让**下一次 Arm** 一定从地址 0 开始写，保证每段录制的起始地址确定、可预测（`Done_s` 下可以直接 Arm 重新录制，见 u3-l2）。若不在 Done 清 0，下一段录制的起点就会依赖上一段结束时指针停在哪，软件读出时无法对齐。

**练习 2**：如果 `In_Vld` 长时间为 0（样本断流），`AdrCnt_2` 会怎样？写入端会发生什么？

**答案**：`In_Vld(1)=0` 时进入「既不清 0 也不递增」的默认保持分支，`AdrCnt_2` 原地不动。同时存储器写使能 `Mem_Wr` 也由 `In_Vld(2)` 派生（[hdl/data_rec.vhd:302-306](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L302-L306)），所以断流期间既不推进指针也不写存储，不会把脏数据写进缓冲。

---

### 4.2 采样计数器 SplCnt 与录制长度配置

#### 4.2.1 概念说明

地址计数器是「环形的空间指针」，而 **`SplCnt` 是「单调递增的时间长度计数器」**——它回答「这一段录制到目前为止一共采了多少个有效样本」。它只被用来判断一件事：**后触发够不够了，可以结束录制了吗？**

这里有一个精妙的设计：录制长度由两个寄存器配置——

- `PreTrigSpls`：前触发样本数（触发**之前**要保留多少个样本）；
- `TotalSpls`：整段录制的总样本数（前触发 + 后触发）。

那么后触发样本数 = `TotalSpls - PreTrigSpls`。乍看似乎需要两个计数器（一个数前触发、一个数后触发），但 `data_rec` 只用了一个 `SplCnt`，秘密在于：**进入等待触发状态时，把 `SplCnt` 直接「预置」到 `PreTrigSpls+1`**，相当于宣布「前触发那部分我已经数过了」，之后让它一路数到 `TotalSpls` 即可。

`SplCnt_2` 的位宽比 `AdrCnt_2` 多 1 位（[hdl/data_rec.vhd:108](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L108)）：

```vhdl
SplCnt_2 : unsigned(Mem_Adr'high+1 downto 0);   -- 比 AdrCnt 多 1 位
```

这与 entity 端口一致：`TotalSpls` 的位宽是 \(\lceil\log_2(\text{MemoryDepth\_g})\rceil+1\) 位，比 `PreTrigSpls` 多 1 位（[hdl/data_rec.vhd:54-55](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L54-L55)）。多这 1 位是为了让 `TotalSpls` 能表示「正好等于缓冲深度」的满窗录制（例如 `MemoryDepth_g=128` 时 `TotalSpls` 可达 128，需要 8 位）。

#### 4.2.2 核心流程

`SplCnt` 的更新规则（[hdl/data_rec.vhd:289-294](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L289-L294)）只有两条分支：

```
if State == WaitTrig:
    SplCnt_next = PreTrigSpls + 1      # 预置：前触发已数过
elif In_Vld(1) == 1:
    SplCnt_next = SplCnt + 1           # 其他录制态：随有效样本递增
# else: 保持
```

把它和状态机对照看（u3-l2），`SplCnt` 在各状态下的行为是：

| 状态 | `SplCnt_2` 行为 | 是否被状态机读取 |
| --- | --- | --- |
| `Idle_s` | 复位为 0（`p_seq`） | 否 |
| `PreTrig_s` | 随有效样本递增（但其值**无意义**，会被 WaitTrig 覆盖） | 否 |
| `WaitTrig_s` | **持续被预置**为 `PreTrigSpls+1` | 否 |
| `PostTrig_s` | 随有效样本递增：`PreTrigSpls+1, PreTrigSpls+2, …` | **是**（判断 `>= TotalSpls`） |
| `Done_s` | 继续递增（无意义） | 否 |

后触发结束的判定在状态机里（[hdl/data_rec.vhd:264-268](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L264-L268)）：

```vhdl
when PostTrig_s =>
    if r.SplCnt_2 >= unsigned(TotalSpls) then
        v.State_2 := Done_s;
        v.Done(2) := '1';
    end if;
```

为什么预置值是 `PreTrigSpls+1` 而不是 `PreTrigSpls`？因为触发**那一个样本**本身也要算进总数。把 `SplCnt` 在触发点设为 `PreTrigSpls+1`，等价于声明：「到触发样本为止，我已经记录了 `PreTrigSpls` 个前触发样本 + 1 个触发样本」。之后每采一个后触发样本就 `+1`，直到 `>= TotalSpls`。于是整段录制的样本数为：

\[
\text{录制总数} = \underbrace{\text{PreTrigSpls}}_{\text{前触发}} + \underbrace{(\text{TotalSpls} - \text{PreTrigSpls})}_{\text{后触发}} = \text{TotalSpls}
\]

一个计数器，靠「预置起点」同时管住了前/后两段，这是本讲最值得记住的设计技巧。

#### 4.2.3 源码精读

采样计数器逻辑（[hdl/data_rec.vhd:289-294](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L289-L294)）：

```vhdl
-- Sample Counter handling
if r.State_2 = WaitTrig_s then
    v.SplCnt_2 := unsigned('0' & PreTrigSpls) + 1;
elsif r.In_Vld(1) = '1' then
    v.SplCnt_2 := r.SplCnt_2 + 1;
end if;
```

注意预置表达式里的 `'0' & PreTrigSpls`：它把 `PreTrigSpls`（\(\lceil\log_2(\text{MemoryDepth\_g})\rceil\) 位）高位补 0 扩展成 `SplCnt_2` 的宽度（多 1 位），再做 `+1`。这一步必须扩位，否则 `PreTrigSpls` 取到最大值时 `+1` 会溢出。例如 `MemoryDepth_g=128` 时 `PreTrigSpls` 最大 127，`127+1=128` 需要 8 位，不扩位就会回卷成 0。

后触发结束判定前面已贴（[hdl/data_rec.vhd:264-268](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L264-L268)）。`p_seq` 里 `SplCnt_2` 同步复位为 0（[hdl/data_rec.vhd:375](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L375)）。

#### 4.2.4 代码实践

**实践目标**：验证「一个计数器管两段」的设计，并手算后触发样本数。

设 `MemoryDepth_g=128`、`PreTrigSpls=30`、`TotalSpls=100`。

1. 在 [hdl/data_rec.vhd:290-291](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L290-L291) 找到预置语句，确认进入 `WaitTrig_s` 时 `SplCnt_2` 被设为 \(30+1=31\)。
2. 在 [hdl/data_rec.vhd:264-268](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L264-L268) 找到结束判定，确认 `PostTrig_s` 在 `SplCnt_2 >= 100` 时进入 `Done_s`。
3. 手算：`SplCnt_2` 在 PostTrig 取值序列是 \(31, 32, \ldots, 100\)。后触发实际记录了多少个样本？

**预期结果**：

- 预置值 = 31。
- 后触发样本数 = `TotalSpls - PreTrigSpls` = \(100 - 30 = 70\) 个。
- 验证：前触发 30 + 后触发 70 = 100 = `TotalSpls`，总数吻合。

#### 4.2.5 小练习与答案

**练习 1**：把 `PreTrigSpls` 设为 0、`TotalSpls` 设为 50。`SplCnt_2` 在触发点被预置成多少？整段录制里有几个前触发样本、几个后触发样本？

**答案**：预置值 = \(0 + 1 = 1\)。整段录制有 0 个前触发样本、50 个后触发样本（`SplCnt` 从 1 数到 50）。这正是「纯后触发」模式。

**练习 2**：`SplCnt` 在 `PreTrig_s` 也会随样本递增，这段递增的值为什么是「无意义」的？它会被什么覆盖？

**答案**：因为状态机在 `PreTrig_s` 从不读取 `SplCnt`（前触发是否结束是由 `AdrCnt_2 = PreTrigSpls-1` 判定的，见 4.3）。一旦状态进入 `WaitTrig_s`，`SplCnt` 立刻被预置成 `PreTrigSpls+1`，把 PreTrig 阶段累积的旧值整个覆盖掉。所以 PreTrig 期间的 `SplCnt` 只是在「空转」，真正起作用的是从 WaitTrig 预置之后的值。

---

### 4.3 计数器如何驱动状态机迁移（协同推演）

#### 4.3.1 概念说明

把 4.1 和 4.2 合起来看：`AdrCnt` 和 `SplCnt` 不是孤立运转的，它们是状态机迁移的**判据**。本节把两个计数器放回五状态机（u3-l2）里，看清每条迁移边是「读了哪个计数器、读到什么值」触发的，并处理一个重要特例：`PreTrigSpls = 0`。

回顾两个判据在状态机里的位置（[hdl/data_rec.vhd:246-276](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L246-L276)）：

- **PreTrig → WaitTrig**：判据来自 `AdrCnt`（`AdrCnt_2 = PreTrigSpls - 1` 且 `In_Vld(1) = 1`）；
- **PostTrig → Done**：判据来自 `SplCnt`（`SplCnt_2 >= TotalSpls`）。

也就是说：**前触发长度由地址计数器量出，后触发长度由采样计数器量出**。这是因为前触发阶段指针从 0 开始线性增长，正好可以用「指针到达 `PreTrigSpls-1`」表示「前触发样本已凑齐」；而一旦进入 WaitTrig，指针就改成环形循环（不再能反映「凑齐了几个」），所以后触发改用专门的单调计数器 `SplCnt`。

#### 4.3.2 核心流程

**PreTrig 结束判据**（[hdl/data_rec.vhd:251-258](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L251-L258)）：

```vhdl
when PreTrig_s =>
    -- Skip if no pre trigger is required
    if unsigned(PreTrigSpls) = 0 then
        v.State_2 := WaitTrig_s;
    -- Pre trigger recorded
    elsif (r.AdrCnt_2 = unsigned(PreTrigSpls)-1) and (r.In_Vld(1) = '1') then
        v.State_2 := WaitTrig_s;
    end if;
```

这里有一个必须单独处理的**特例**：`PreTrigSpls = 0`。

为什么不能直接用 `r.AdrCnt_2 = unsigned(PreTrigSpls)-1` 这一条来判断？因为 `PreTrigSpls` 是 `unsigned`，当它为 0 时，`unsigned(PreTrigSpls) - 1` 会**下溢（underflow）**成全 1（即缓冲深度上限），这个值永远不会等于刚清 0 的 `AdrCnt_2`，结果就是「永远凑不齐前触发、卡死在 PreTrig」。所以代码把 `PreTrigSpls = 0` 单独摘出来，直接跳进 `WaitTrig_s`——即「不需要前触发，立刻开始等触发」。

> 提示：这是无符号数运算的经典陷阱。`unsigned("0") - 1 = "1...1"`，不是 -1。代码用 `if unsigned(PreTrigSpls) = 0` 提前拦截，避免下溢。

整段录制里两个计数器的协同时间线（按「有效样本」计，忽略流水填充延迟）：

```
Arm ──► PreTrig ──► WaitTrig ──► PostTrig ──► Done
        AdrCnt:0→PreTrigSpls-1   AdrCnt: 环形继续      AdrCnt: 环形继续
        (SplCnt 空转)            SplCnt:=PreTrigSpls+1 SplCnt:+1→…→TotalSpls
                                 ↑ 触发发生在此态
```

#### 4.3.3 源码精读：触发时刻两个计数器的值

触发发生在 `WaitTrig_s`，判据是 `TrigNow_2 = '1'`（触发源如何合成见 u4-l1，本讲只关心触发那一刻两个计数器各自是多少）。状态机分支（[hdl/data_rec.vhd:259-263](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L259-L263)）：

```vhdl
when WaitTrig_s =>
    if TrigNow_2 = '1' then
        v.State_2  := PostTrig_s;
        v.Trigger_2 := '1';
    end if;
```

触发那一拍（`r.State_2 = WaitTrig_s` 且 `TrigNow_2 = 1`）：

- **`SplCnt_2`**：因为 `r.State_2` 仍是 `WaitTrig_s`，采样计数器逻辑（4.2）本拍会把它（重新）预置成 `PreTrigSpls + 1`。所以在触发时刻读到的 `SplCnt_2` 就是 `PreTrigSpls + 1`——这就是 4.2 说的「触发样本计为第 `PreTrigSpls+1` 个」。
- **`AdrCnt_2`**：它从进入 WaitTrig 起就一直在环形递增，所以触发时刻的值取决于「等触发等了多久」（等了几个有效样本，指针就走了几步）。这个值随后被用来计算环形缓冲里第一个有效样本的地址 `FirstSplAddr`（见 [hdl/data_rec.vhd:308-321](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L308-L321)，完整推导留到 u3-l5）。

`Trigger_2` 这一拍被置 1，下一拍 `r.Trigger_2 = '1'` 时，`FirstSpl` 用此时的 `AdrCnt_2` 做一次减法（[hdl/data_rec.vhd:309-321](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L309-L321)）。这里有个流水对齐细节：`FirstSpl` 读的是「触发后一拍」的 `AdrCnt_2`（已经多递增了一次），精确的样本对齐由 Stage 2→3 的流水延迟吸收，本讲只点到为止，u3-l5 会把 `FirstSpl` 与非二次幂深度的回绕一次讲透。

#### 4.3.4 代码实践

**实践目标**：定位 `PreTrigSpls=0` 的特例分支，并理解它为何必须存在。

1. 打开 [hdl/data_rec.vhd:251-258](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L251-L258)。
2. 假设删掉 `if unsigned(PreTrigSpls) = 0 then ... ` 这一段，只保留 `elsif`。设 `PreTrigSpls = 0`，手算 `unsigned(PreTrigSpls) - 1` 在 7 位 `unsigned` 下等于多少？`AdrCnt_2`（刚被清 0）可能等于它吗？
3. 再对照 u6-l2 提到的 case4——该用例正是用非法/边界配置（如 `PreTrigSpls > TotalSpls`、`TotalSpls = 0`）来验证记录器能否安全恢复，与本处的 `PreTrigSpls = 0` 边界处理一脉相承。

**预期结果**：

- 7 位 `unsigned` 下 `0 - 1 = "1111111"` = 127（对 `MemoryDepth_g=128` 而言即 `MemoryDepth_g-1`）。
- `AdrCnt_2` 从 0 起步，要等于 127 必须先把整个缓冲写满一圈，这显然不是「0 个前触发样本」的预期行为；若无特例拦截，记录器会在 PreTrig 卡住或晚一整圈才进入 WaitTrig。所以特例分支不可省。

> 说明：本实践为源码阅读与手算型，「待本地验证」的部分可借 u1-l3 的仿真平台，构造一个 `PreTrigSpls=0` 的用例观察是否立即进入 WaitTrig。

#### 4.3.5 小练习与答案

**练习 1**：前触发结束用 `AdrCnt` 判断、后触发结束用 `SplCnt` 判断。为什么后触发不能也用 `AdrCnt` 来判断？

**答案**：因为进入 WaitTrig 后 `AdrCnt` 改为环形循环（到 `MemoryDepth_g-1` 回绕），它不再单调反映「采了几个样本」，无法用来度量后触发长度。后触发可能跨越回绕点（指针从 100 走到 30 这种情况），所以必须用一个独立、单调递增、不回绕的计数器 `SplCnt`。

**练习 2**：触发那一刻 `SplCnt_2` 一定是 `PreTrigSpls + 1` 吗？为什么？

**答案**：是的。因为触发判据 `TrigNow_2=1` 只在 `r.State_2 = WaitTrig_s` 时生效，而采样计数器逻辑在 `WaitTrig_s` 每一拍都把 `SplCnt_2` 预置成 `PreTrigSpls+1`（[hdl/data_rec.vhd:290-291](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L290-L291)）。所以无论在 WaitTrig 等了多久、哪一拍触发，触发瞬间的 `SplCnt_2` 恒为 `PreTrigSpls+1`，与等待时长无关。

---

## 5. 综合实践

**任务**：设 `MemoryDepth_g = 128`、`PreTrigSpls = 30`、`TotalSpls = 100`，并假设数据持续有效（`In_Vld` 恒为 1）、触发在 `WaitTrig` 阶段当 `AdrCnt_2` 走到 40 时到来。逐步推演从 Arm 到 Done 全过程 `AdrCnt_2` 与 `SplCnt_2` 的变化，并指出**触发时刻**两者的值。

> 约定：下表记录的是**寄存器值 `r.AdrCnt_2` / `r.SplCnt_2`**（即 Stage 2 的值，正是状态机 `case r.State_2` 实际读取的对象），按「有效样本步」推进，省略流水填充的最初几拍空转。

| 阶段 | `r.State_2` | `r.AdrCnt_2` | `r.SplCnt_2` | 发生了什么（引用代码） |
| --- | --- | --- | --- | --- |
| Idle，收到 Arm | `Idle_s` | 0 | 0 | Arm=1，下一拍进 PreTrig；AdrCnt 此时被清 0（[L279-280](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L279-L280)） |
| PreTrig 中 | `PreTrig_s` | 0 → 1 → … → 29 | 0 → 1 → … → 29 | 两计数器同随有效样本递增；SplCnt 此段值无意义（[L281-286](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L281-L286)、[L292-293](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L292-L293)） |
| PreTrig 末 | `PreTrig_s` | **29** | 29 | 命中 `AdrCnt_2 = PreTrigSpls-1 = 29` 且 `In_Vld(1)=1`，下一拍进 WaitTrig（[L256-257](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L256-L257)） |
| 进入 WaitTrig | `WaitTrig_s` | 30 | **31** | SplCnt 被预置为 `PreTrigSpls+1 = 31`（[L290-291](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L290-L291)） |
| WaitTrig 中 | `WaitTrig_s` | 31 → 32 → … → 40 | 31（保持） | AdrCnt 环形递增；SplCnt 每拍被重新预置成 31，故保持不变 |
| **触发时刻** | `WaitTrig_s` | **40** | **31** | `TrigNow_2 = 1`，下一拍进 PostTrig、置 `Trigger_2`（[L260-262](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L260-L262)） |
| 进入 PostTrig | `PostTrig_s` | 41 | 31 → 32 | 触发后一拍 `Trigger_2=1`，用此时 `AdrCnt_2=41` 计算 `FirstSpl`（[L309-312](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L309-L312)）；SplCnt 恢复递增 |
| PostTrig 中 | `PostTrig_s` | 42 → 43 → … | 32 → 33 → … → 100 | SplCnt 每有效样本 +1；AdrCnt 继续环形递增 |
| PostTrig 末 | `PostTrig_s` | … | **100** | 命中 `SplCnt_2 >= TotalSpls = 100`，下一拍进 Done、置 Done(2)（[L265-267](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L265-L267)） |
| Done | `Done_s` | 0（复位） | （继续增，无意义） | AdrCnt 归 0，等软件 Ack 或重新 Arm（[L279-280](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L279-L280)） |

**触发时刻的答案**：`AdrCnt_2 = 40`，`SplCnt_2 = 31`（= `PreTrigSpls + 1`）。

**自检**：

- 后触发样本数 = `TotalSpls - PreTrigSpls` = \(100 - 30 = 70\)（`SplCnt` 从 31 数到 100，共 70 步）。
- 整段录制总数 = 前触发 30 + 后触发 70 = 100 = `TotalSpls`，与配置一致。
- `FirstSpl`（本讲只算二次幂情况）= 触发后一拍的 `AdrCnt_2 - PreTrigSpls` = \(41 - 30 = 11\)，即读出时应从地址 11 开始把环形缓冲展开成线性序列（完整含义见 u3-l5）。

> 若想在仿真中实测这张表，可按 u1-l3 跑 `top_tb`（`MemoryDepth_g=32` 这组），在波形里对 `r.AdrCnt_2`、`r.SplCnt_2`、`r.State_2` 三个信号打 group，观察 PreTrig→WaitTrig→PostTrig→Done 各边界上它们的取值是否与上表吻合。

## 6. 本讲小结

- **`AdrCnt` 是环形写指针**：Idle/Done 清 0；录制中遇有效样本递增，到 `MemoryDepth_g-1` 回绕；受 `In_Vld(1)` 门控，断流时不动。它驱动存储器写地址 `Mem_Adr`（经 `AdrCnt_3` 对齐）。
- **`SplCnt` 是单调长度计数器**：在 `WaitTrig_s` 被持续预置为 `PreTrigSpls + 1`，在 PostTrig 随有效样本递增；它**只被** PostTrig→Done 的判定读取（`>= TotalSpls`）。
- **一个计数器管两段**：靠「预置起点 = `PreTrigSpls+1`」让 `SplCnt` 同时表示前触发已数过 + 后触发正在数，整段总数恰为 `TotalSpls`。预置表达式 `'0' & PreTrigSpls` 的高位补 0 是为避免 `+1` 溢出。
- **前触发由 `AdrCnt` 量、后触发由 `SplCnt` 量**：因为进 WaitTrig 后 `AdrCnt` 改为环形循环，不再单调，无法度量后触发长度。
- **`PreTrigSpls = 0` 是必须单独处理的特例**：否则 `unsigned(PreTrigSpls)-1` 下溢成全 1，记录器会卡在 PreTrig；代码用 `if unsigned(PreTrigSpls) = 0` 提前拦截，直接进 WaitTrig。
- **触发时刻的取值是确定的**：`SplCnt_2` 恒为 `PreTrigSpls+1`（与等待时长无关），`AdrCnt_2` 则取决于等触发等了多久，并随后用于计算 `FirstSplAddr`。

## 7. 下一步学习建议

- **u3-l5（非二次幂深度与 FirstSplAddr）**：本讲多次提到 `FirstSpl` 用触发时刻的 `AdrCnt_2` 减 `PreTrigSpls` 得到。下一讲会讲清当 `MemoryDepth_g` 不是二次幂时，这个减法可能产生借位，需要再加回 `MemoryDepth_g` 才能得到正确的环形起始地址——即 [hdl/data_rec.vhd:311-320](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L311-L320) 的 `else` 分支。
- **u4-l1（触发源总览与 TrigEna 掩码）**：本讲把 `TrigNow_2` 当成「触发来了」的黑盒，下一讲拆开它是如何由外部/软件/自触发三种源经 `TrigEna` 掩码合成、并由 `In_Vld(1)` 门控的。
- **建议动手**：在仿真波形里同时观察 `r.AdrCnt_2`、`r.SplCnt_2`、`r.State_2` 三个信号，对照本讲第 5 节的表格逐拍核对，这是巩固「计数器驱动状态机」最直观的方式。

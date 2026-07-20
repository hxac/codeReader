# 两进程法与 Stage0-3 流水线

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 VHDL「两进程法（two-process method）」的模板：为什么把一个时序电路拆成一个组合进程 `p_comb` 和一个时序进程 `p_seq`，以及 `v := r`、`r_next <= v`、`r <= r_next` 这三句话各自的作用。
- 读懂 `data_rec` 里 `data_rec_r` 这个 record 把所有寄存器打包在一起的设计，并能按「输入寄存器 / 状态 / 配置」三类把字段分类。
- 逐级讲清 Stage0、Stage1、Stage2、Stage3 各自负责哪些计算，以及 `In_Vld`、`Trig_In`、`Data`、`Done` 这些信号是如何在流水级之间被「搬运」的。
- 解释命名后缀的含义：为什么信号叫 `Trigger_2`、`MemWr_3`、`FirstSpl_3`，这个数字代表它属于第几级流水、对齐到哪个时钟拍。

本讲只讲「代码是怎么组织的、流水线是怎么搭的」，**不**展开状态机的具体迁移条件（那是上一讲 u3-l2 的内容），也**不**展开非二次幂地址的细节（那是下一讲 u3-l5 的内容）。

## 2. 前置知识

在开始前，你需要先具备以下概念（已在前序讲义建立）：

- **寄存器传输级（RTL）与时钟**：VHDL 综合成的是触发器（flip-flop）和组合逻辑。`process(Clk)` 且在 `rising_edge(Clk)` 里赋值的信号会被综合成触发器，每个时钟上升沿更新一次；不带时钟的赋值综合成连线/组合逻辑。
- **两进程法**：一种把「下一拍应该变成什么（组合计算）」和「在时钟沿把下一拍搬进当前拍（寄存）」严格分开的写法。它的好处是把所有时序元素集中在一个 record 里，组合逻辑里**只**用变量 `v` 做「读 r、算 v」，降低出错概率。
- **record 类型**：VHDL 的结构体，把多个不同类型的信号字段打包成一个整体，可以用 `r.State_2`、`r.Data_3(i)` 这样的点号访问。
- **上一讲（u3-l2）的状态机**：记录器有 `Idle_s → PreTrig_s → WaitTrig_s → PostTrig_s → Done_s` 五个状态，迁移逻辑写在 `p_comb` 的 `case r.State_2` 里。本讲要解释的就是：这些迁移逻辑为什么被放在「Stage 2」这一段，而不是别处。
- **`log2ceil` / `log2`**：来自 `psi_common_math_pkg` 的取对数函数，用于从 generic 推导地址位宽。

如果你对「触发器 vs 组合逻辑」还不熟，建议先补一下数字电路基础再回来。

## 3. 本讲源码地图

本讲只涉及**一个**源码文件，但它是整个 IP 最核心的文件：

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [hdl/data_rec.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd) | 数据记录器核心 RTL | record 定义、`p_comb`、`p_seq`、Stage0~3 流水线 |

文件内部的「地标」如下，阅读时按这个顺序找：

- 第 94–123 行：`data_rec_r` record 类型 + `r`、`r_next` 两个信号。
- 第 142–361 行：组合进程 `p_comb`（本讲主角）。
- 第 366–387 行：时序进程 `p_seq`。
- 第 393 行：`Trig_Out` 输出赋值。

## 4. 核心概念与源码讲解

### 4.1 两进程法与 `data_rec_r` 记录类型

#### 4.1.1 概念说明

「两进程法」是 PSI 的 `psi_common` 系列代码里几乎统一的编码风格。它的核心思想是：

> **把一个时序电路拆成两个进程。一个进程负责「计算下一拍的值」，另一个进程负责「在时钟沿把下一拍搬进来」。所有寄存器统一打包在一个 record 里。**

为什么要这么写？

1. **职责单一**：时序进程 `p_seq` 极其简短，只做 `r <= r_next` 和复位，几乎不会写错；所有「业务逻辑」都集中在组合进程 `p_comb` 里。
2. **避免意外锁存器（latch）**：组合进程第一行永远是 `v := r;`，意思是「默认保持不变」，只有需要改的地方才覆盖 `v` 的某个字段。这等价于「每个寄存器都默认保持自己的值」，综合器不会推断出锁存器。
3. **可读性强**：所有寄存器字段集中在 record 定义里，一眼能看清整个模块有多少状态。

`data_rec` 把自己所有的寄存器打包成 record 类型 `data_rec_r`，并声明了两个该类型的信号 `r`（当前拍）和 `r_next`（下一拍）。

#### 4.1.2 核心流程

两进程法的数据流可以用下面这个循环描述：

```
        ┌─────────────────────────────────────────────────┐
        │  p_comb（组合进程，无时钟）                       │
        │  1. v := r;                  -- 默认保持         │
        │  2. 根据 r 和输入，修改 v 的字段                  │
        │  3. r_next <= v;             -- 输出下一拍        │
        └─────────────────────────────────────────────────┘
                           │ r_next
                           ▼
        ┌─────────────────────────────────────────────────┐
        │  p_seq（时序进程，时钟 Clk）                      │
        │  if rising_edge(Clk) then                        │
        │      r <= r_next;            -- 采样下一拍        │
        │      if Rst = '1' then      -- 同步复位覆盖       │
        │          r.State_2 <= Idle_s; ……                 │
        │      end if;                                     │
        │  end if;                                         │
        └─────────────────────────────────────────────────┘
                           │ r
                           ▼
                  （回到 p_comb 的敏感表）
```

注意 `p_comb` 的敏感表里同时有 `r` 和所有外部输入（`In_Vld`、`Arm`、`PreTrigSpls` ……）。只要 `r` 或任一输入变化，`p_comb` 就重新计算一遍 `r_next`。综合时，这个组合进程会被展开成一大片组合逻辑，其输出 `r_next` 在 `Clk` 上升沿被打入 `r`。

#### 4.1.3 源码精读

先看 record 类型定义本身（注释和字段分组很清楚）：

[data_rec.vhd:94-123 — data_rec_r record 与 r/r_next 信号](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L94-L123)

这段代码把所有寄存器分成四组：

| 分组 | 代表字段 | 含义 |
|---|---|---|
| 输入寄存器 | `In_Vld(0 to 2)`、`Trig_In(0 to 2)`、`Data_0..3` | 对外部输入做多级寄存，构成流水线 |
| 状态 | `State_2`、`Trigger_2`、`AdrCnt_2/3`、`SplCnt_2`、`Done(2 to 3)`、`MemWr_3`、`TrigCnt_3`、`DoneTime_3`、`FirstSpl_3`、`StInRange_1`、`StInRangeLast_1`、`LastRecCnt_2` | 状态机与各类计数器 |
| 配置 | `SwTrigPending_2`、`ExtTrigPending_2` | 触发的「挂起（pending）」标志 |
| 触发输出 | `Trig_Out` | 转发内部触发脉冲 |

这里有两个 VHDL 小技巧值得指出：

1. `In_Vld` 用的是数组 `std_logic_vector(0 to 2)`，而下标 `0 to 2` 表示一个**移位寄存器**，`(0)` 是最新一拍、`(2)` 是三拍前。`Done` 用的是 `std_logic_vector(2 to 3)`，注意它**不是**状态机的状态，而是一个 2 拍的脉冲移位寄存器（下标从 2 开始是为了和流水级编号对齐）。
2. `Data_0..3` 不是数组下标，而是**四个独立的 record 字段**，每个字段又是一个数组 `Data_t(NumOfInputs_g-1 downto 0)`。这样命名是为了显式表达「这是第 0/1/2/3 级流水线上的数据」。

最关键的两行信号声明在最后：

```vhdl
signal r, r_next : data_rec_r;
```

`r` 是「当前拍」（被 `p_seq` 在时钟沿更新），`r_next` 是「下一拍」（由 `p_comb` 算出来）。两者类型完全相同。

> 注意：`State_t` 类型（第 92 行 `type State_t is (Idle_s, PreTrig_s, WaitTrig_s, PostTrig_s, Done_s);`）定义在 record 之外，是独立的枚举类型，record 里的 `State_2` 字段才用它。这是上一讲 u3-l2 讲过的内容。

#### 4.1.4 代码实践

**实践目标**：在阅读源码前，先靠 record 定义建立「这个模块有多少寄存器」的直觉。

**操作步骤**：

1. 打开 [hdl/data_rec.vhd:94-123](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L94-L123)。
2. 数一下 `Data_0`、`Data_1`、`Data_2`、`Data_3` 一共有几级（答案：4 级）。
3. 数一下 `In_Vld(0 to 2)` 一共有几拍（答案：3 拍）。
4. 问自己一个问题：**为什么数据要走 4 级，而有效信号 `In_Vld` 只走 3 拍？** 先把你的猜测写下来。（这个问题在 4.4 节会给出完整答案，但你先自己想一想会更有收获。）

**需要观察的现象**：你会发现 record 里没有任何一个字段叫 `Data`（不带后缀）——所有数据字段都带 `_0..3` 后缀；而 `In_Vld`/`Trig_In`/`Done` 用的是数组下标而不是后缀。这种「带后缀 vs 用下标」的混用是有意为之，反映了两种不同的流水线实现方式。

**预期结果**：你能说清「`Data_0..3` 是四个独立字段的流水，`In_Vld(0..2)` 是一个数组实现的移位寄存器」。

#### 4.1.5 小练习与答案

**练习 1**：record 里 `Done` 的类型是 `std_logic_vector(2 to 3)`，为什么下标从 2 开始而不是从 0 开始？

**参考答案**：为了让下标和流水级编号一致。`Done(2)` 在 Stage 2 的状态机里被置位（进入 `Done_s` 时），`Done(3)` 是它延迟一拍后的输出。用 `2 to 3` 而不是 `0 to 1`，是为了让读者一眼看出「这个脉冲来自 Stage 2，在 Stage 3 输出」。

**练习 2**：如果把 `signal r, r_next : data_rec_r;` 改成两个进程各自定义局部变量，会有什么问题？

**参考答案**：变量是进程局部的，无法在两个进程间共享。`p_comb` 算出的下一拍值必须通过信号 `r_next` 传给 `p_seq`，`p_seq` 再把它打入 `r` 供 `p_comb` 下一轮读取。这正是两进程法「用两个信号 `r`/`r_next` 桥接两个进程」的原因。

---

### 4.2 组合进程 `p_comb`：`v := r` 模板与流水搬运

#### 4.2.1 概念说明

`p_comb` 是整个模块的「大脑」，几乎所有业务逻辑都写在这里。它的结构高度模板化：

1. 声明一个**局部变量** `v : data_rec_r`（变量赋值是即时的，不像信号赋值要到进程结束才生效）。
2. 进程开头 `v := r;`，让 `v` 默认等于当前拍的所有寄存器值。
3. 按流水级顺序（Stage0 → Stage1 → Stage2 → Stage3）修改 `v` 里需要改的字段。
4. 进程末尾 `r_next <= v;`，把算好的下一拍交给时序进程。

用变量 `v` 而不是直接对信号赋值，是因为在一个进程内变量赋值立即生效，**后续的代码可以读到刚才修改过的 `v` 字段**——这对「在同一拍内先算触发、再用触发结果更新状态机」这种顺序计算至关重要。

#### 4.2.2 核心流程

`p_comb` 内部的执行顺序（从上到下、逐行执行）：

```
1. v := r;                              -- 默认保持
2. Pipe Handling（流水搬运）             -- 把移位寄存器整体左移一拍
3. Stage 0：采入新输入                   -- In_Vld(0)、Trig_In(0)、Data_0
4. Stage 1：Data_1 流水 + 自触发范围判定
5. Stage 2：Data_2 流水 + 触发裁决 + 状态机 + 计数器
6. Stage 3：Data_3 流水 + 存储写使能 + FirstSpl + 计数器
7. 输出赋值（State、Mem_Data、Mem_Adr、Mem_Wr ……）
8. r_next <= v;
```

特别注意第 2 步「流水搬运」和第 3 步「采入新输入」的**顺序**：先把整个移位寄存器左移一格（旧值往后挪），再在最前面（下标 0）写入新值。这是移位寄存器的标准写法。

#### 4.2.3 源码精读

先看进程头和变量声明。注意敏感表里列出了 `r` 和所有外部输入：

[data_rec.vhd:142-150 — p_comb 的敏感表与变量声明](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L142-L150)

进程里声明了 5 个变量：`v` 是「下一拍」的完整 record；`StEnter_2`、`StExit_2`、`StTrig_2`、`TrigNow_2` 是 Stage 2 触发裁决用的真·临时变量（它们**不**进 record，因为不需要跨拍保持）。

第一行永远是 `v := r;`：

[data_rec.vhd:152-153 — 默认保持 v := r](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L152-L153)

这一行是两进程法的「保险丝」：它保证任何没被后续代码覆盖的 `v` 字段都保持原值，等价于「寄存器默认保持」，综合器绝不会推断出锁存器。

接下来是流水搬运（Pipe Handling），用范围切片实现「整体左移」：

[data_rec.vhd:155-158 — In_Vld/Trig_In/Done 的移位搬运](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L155-L158)

这三行的套路完全一样，以 `In_Vld` 为例：

```vhdl
v.In_Vld(v.In_Vld'low+1 to v.In_Vld'high) := r.In_Vld(r.In_Vld'low to r.In_Vld'high-1);
```

含义是：`v.In_Vld(1 to 2) := r.In_Vld(0 to 1)`，即把第 0 拍挪到第 1 拍、第 1 拍挪到第 2 拍。注意这里用 `'low`/`'high` 属性而不是写死数字，是为了和 record 定义里的下标范围（`0 to 2`、`2 to 3`）自动匹配——`Done` 的范围是 `2 to 3`，同一行代码也能正确工作。

搬运完之后，Stage 0 再把**新鲜输入**写到下标 0（见 4.4.3 节）。这样「先挪后填」就完成了一次移位。

进程末尾把算好的 `v` 交出去：

[data_rec.vhd:358-360 — r_next <= v 把下一拍交给时序进程](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L358-L360)

#### 4.2.4 代码实践

**实践目标**：亲手验证「`v := r` + 变量即时赋值」带来的顺序计算能力。

**操作步骤**：

1. 阅读 [data_rec.vhd:243-276](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L243-L276)（Stage 2 的状态机 `case r.State_2`）。
2. 注意 `WaitTrig_s` 分支里：先计算 `TrigNow_2`（第 225–228 行），再在 `case` 里用 `if TrigNow_2 = '1'` 决定是否迁移到 `PostTrig_s`（第 260 行）。这里 `TrigNow_2` 是**变量**，赋值后立即生效，所以下面的 `case` 能读到它。
3. 思考：如果把 `TrigNow_2` 改成信号（比如 `signal TrigNow_2 : std_logic`），这个「先算触发、再据此迁移状态」的逻辑还能在同一拍内完成吗？

**需要观察的现象**：你会看到变量赋值的「立即生效」是让 Stage 2 能在**同一拍内**完成「裁决触发 → 推进状态机」的关键。

**预期结果**：你能解释「为什么 `TrigNow_2`、`StEnter_2` 这些是进程内的 variable 而不是 record 字段或 signal」——因为它们是同一拍内的中间结果，不需要跨拍保持，用变量最自然。

**待本地验证**：以上是源码阅读型实践，无需运行仿真即可完成。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `v := r;`（第 153 行），综合器会报什么错或产生什么问题？

**参考答案**：`v` 是变量，未赋值的字段取默认值（`std_logic` 默认 `'U'`）。这意味着任何没被显式覆盖的 `v` 字段每拍都会被写成一个无效值，整个模块失效；而且因为 `v` 是变量不是信号，综合器不会报锁存器警告，bug 会很隐蔽。这就是为什么 `v := r;` 必须是第一行。

**练习 2**：流水搬运用 `'low`/`'high` 属性而不是写死 `0`、`1`、`2`，有什么好处？

**参考答案**：让同一段移位代码能自动适配不同的数组范围。`In_Vld` 是 `0 to 2`、`Done` 是 `2 to 3`，写死下标就要为每个信号写不同代码；用属性则三段共用同一行模板，减少出错。

---

### 4.3 时序进程 `p_seq`：`r <= r_next` 与同步复位

#### 4.3.1 概念说明

`p_seq` 是两进程法里「极简」的那个进程。它的全部职责只有两件：

1. 在 `Clk` 上升沿，把 `r_next`（组合进程算好的下一拍）搬进 `r`（当前拍）。
2. 在 `Rst = '1'` 时，用同步复位值覆盖 `r` 的部分字段。

因为它这么简单，所以几乎不可能写错——所有的复杂度都被推到了 `p_comb` 里。这是两进程法的核心收益。

#### 4.3.2 核心流程

```
process(Clk)
    if rising_edge(Clk) then
        r <= r_next;          -- 先整体搬入下一拍
        if Rst = '1' then     -- 再用复位值覆盖需要复位的字段
            r.State_2 <= Idle_s;
            r.AdrCnt_2 <= 0;
            ……
        end if;
    end if;
```

注意这里的**顺序**：先 `r <= r_next;`（整体搬入），再 `if Rst` 用具体值覆盖。在 VHDL 里，同一个进程内对同一信号的多次赋值，**最后一次生效**。所以复位值会「赢过」`r_next` 的值。这是一种紧凑写法，等价于「正常时搬入下一拍，复位时强制清零」。

#### 4.3.3 源码精读

[data_rec.vhd:366-387 — p_seq 时序进程与同步复位](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L366-L387)

读这段代码时，注意三个细节：

1. **同步复位**：复位写在 `if rising_edge(Clk)` **里面**，所以是同步复位（只有时钟沿才生效），不是异步复位。这意味着复位信号 `Rst` 不在敏感表里（敏感表只有 `Clk`）。
2. **并非所有字段都被复位**：仔细看复位列表，被显式复位的字段是 `In_Vld`、`Trig_In`、`State_2`、`AdrCnt_2`、`SplCnt_2`、`MemWr_3`、`Trigger_2`、`FirstSpl_3`、`SwTrigPending_2`、`Done`、`TrigCnt_3`、`DoneTime_3`、`ExtTrigPending_2`、`LastRecCnt_2`。**没有**被复位的有 `Data_0..3`、`StInRange_1`、`StInRangeLast_1`、`AdrCnt_3`、`Trig_Out`。这是有意的——数据通路寄存器不复位，因为它们只有在 `In_Vld=1` 时才有意义，而 `In_Vld` 复位为 0 后，旧数据不会被写出（`MemWr_3` 复位为 0）。这是 FPGA 设计里常见的「控制信号复位、数据信号不复位」原则，可节省触发器资源。
3. **`AdrCnt_2` 被复位而 `AdrCnt_3` 没有**：因为 `AdrCnt_3` 只是 `AdrCnt_2` 延迟一拍的副本（第 299 行 `v.AdrCnt_3 := r.AdrCnt_2;`），只要源头 `AdrCnt_2` 复位了，`AdrCnt_3` 几拍后自然清零。

#### 4.3.4 代码实践

**实践目标**：通过对照复位列表，体会「控制信号复位、数据信号不复位」的设计取舍。

**操作步骤**：

1. 打开 [data_rec.vhd:370-385](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L370-L385)。
2. 列出所有**没有**出现在复位列表里的 record 字段。
3. 对每个未复位的字段，问自己：「复位后它会不会被错误地用到？」例如 `Data_3` 复位后是未知值，但 `MemWr_3` 复位为 0，所以存储器不会写出垃圾数据——这就是「数据不复位也安全」的原因。

**需要观察的现象**：你会发现未复制的字段要么是纯数据（`Data_0..3`），要么是被其它复位字段「门控」掉的（如 `AdrCnt_3` 跟随 `AdrCnt_2`）。

**预期结果**：你能说清「为什么 `MemWr_3` 必须复位、而 `Data_3` 可以不复位」——因为写使能是控制信号，决定了数据是否落盘。

**待本地验证**：源码阅读型实践，无需仿真。

#### 4.3.5 小练习与答案

**练习 1**：这个模块用的是同步复位还是异步复位？怎么判断？

**参考答案**：同步复位。判断依据是 `Rst` 判断写在 `if rising_edge(Clk)` 内部，且进程敏感表只有 `Clk`、没有 `Rst`。异步复位的话，敏感表会写成 `process(Clk, Rst)`，且 `if Rst = '1' then` 会写在 `if rising_edge` 之前/外部。

**练习 2**：为什么 `p_seq` 里先写 `r <= r_next;` 再写 `if Rst` 覆盖，而不是写成 `if Rst then r <= 复位值; else r <= r_next; end if;`？

**参考答案**：功能上两者等价，但「先搬入再覆盖」写法更简洁——只需要列出需要复位的字段，其余字段自动从 `r_next` 继承。如果用 `if/else`，`else` 分支仍然是 `r <= r_next;`，反而啰嗦。这是两进程法里常见的紧凑写法。

---

### 4.4 Stage0~3 流水线逐级拆解与命名后缀

#### 4.4.1 概念说明

`data_rec` 不是一个「零延迟」的组合电路，而是一条**四级流水线**。外部输入 `In_Data`、`In_Vld`、`Trig_In` 进入后，要经过 Stage0 → Stage1 → Stage2 → Stage3 四级寄存，最终才出现在存储器写端口 `Mem_Data`、`Mem_Adr`、`Mem_Wr` 上。

为什么非要流水线化？因为单拍内要做的事太多：

- 要判断每个通道样本是否落在自触发范围里（Stage 1 的范围比较）。
- 要检测自触发/外部触发的边沿、裁决三种触发源、推进状态机、更新地址/采样计数器（Stage 2）。
- 要计算首个样本地址、更新触发计数器与 Done 时长计数器（Stage 3）。

如果把这些全塞进一拍，关键路径会非常长，限制了 `Clk` 的最高频率。拆成 4 级后，每级逻辑变浅，时序容易收敛。代价是一个样本要延迟几拍才写到存储器——但只要各级**对齐**，延迟是确定且可补偿的。

> 命名约定的核心：**信号名里的数字后缀表示它属于第几级流水线**。`Trigger_2` 是在 Stage 2 产生/使用的信号；`MemWr_3`、`FirstSpl_3`、`AdrCnt_3` 是在 Stage 3 产生、对齐到存储器写端口的信号；`StInRange_1` 是 Stage 1 的判定结果。看到后缀，就等于看到了这个信号在时间轴上的「位置」。

#### 4.4.2 核心流程

设一个样本 \(S\) 在时钟周期 \(C\) 出现在输入端（`In_Vld=1`、`In_Data=S`）。它在各级寄存器里的「足迹」如下表（「周期 \(C+k\)」表示第 \(k\) 个时钟上升沿之后 `r` 中看到的状态）：

| 周期 | `r.In_Vld(0)` | `r.In_Vld(1)` | `r.In_Vld(2)` | `r.Data_0` | `r.Data_1` | `r.Data_2` | `r.Data_3` | 发生的事 |
|---|---|---|---|---|---|---|---|---|
| \(C+1\) | 1 | – | – | \(S\) | – | – | – | Stage 0 采入 |
| \(C+2\) | – | 1 | – | – | \(S\) | – | – | Stage 1 算范围 `StInRange_1(S)` |
| \(C+3\) | – | – | 1 | – | – | \(S\) | – | Stage 2 裁决触发、推进状态机、算地址 `AdrCnt_2` |
| \(C+4\) | – | – | – | – | – | – | \(S\) | Stage 3：`MemWr_3=r.In_Vld(2)=1`、`AdrCnt_3=AdrCnt_2`，存储器写端口收到 \(S\) |

也就是说，一个样本从输入到出现在存储器写端口，要花 **3 个时钟周期**。在这 3 拍里：

- 数据走 `Data_0 → Data_1 → Data_2 → Data_3`（4 个寄存器，3 次延迟）。
- 有效信号走 `In_Vld(0) → In_Vld(1) → In_Vld(2)`（3 个寄存器），然后在 Stage 3 以 `r.In_Vld(2)` 的形式驱动 `MemWr_3`——这第 4 拍的延迟由 `v.MemWr_3 := r.In_Vld(2);` 这一级寄存补上。

所以数据 `Data_3` 和它的写使能 `MemWr_3` **天然对齐**，都是样本 \(S\) 延迟 3 拍后的结果。这就回答了 4.1.4 节留下的悬念：**数据走 4 级、有效走 3 拍，是因为写使能在 Stage 3 还要多寄存一拍（`MemWr_3`），补齐后两者都是 3 拍延迟，在存储器写端口刚好对齐。**

延迟对齐也可以用一个等式表达。设样本 \(S\) 的写入延迟为 \(D\)，则：

\[
D_{\text{data}} = D_{\text{vld}} = 3 \quad \text{(时钟周期)}
\]

#### 4.4.3 源码精读

下面按 Stage 顺序逐段精读。注意每段代码顶部的注释 `-- *** Stage N ***` 就是流水级的分界。

**Stage 0 —— 采入新输入**

[data_rec.vhd:160-166 — Stage 0：把外部输入寄存进 Data_0/In_Vld(0)/Trig_In(0)](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L160-L166)

这一级把裸输入打一拍：`v.In_Vld(0) := In_Vld;`、`v.Trig_In(0) := Trig_In;`，并用循环把最多 8 路数据搬到 `Data_0(i)`。`In_Data` 这个内部数组是在架构体开头（第 130–137 行）把 `In_Data0..7` 八个端口拼起来的，循环只用前 `NumOfInputs_g` 路。

**Stage 1 —— 数据流水 + 自触发范围判定**

[data_rec.vhd:168-188 — Stage 1：Data_1 流水，并用 Data_0 判定是否落在自触发范围内](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L168-L188)

注意这里用 `r.Data_0`（Stage 0 的结果）来算 `v.StInRange_1`（Stage 1 的结果）。判定逻辑先试 unsigned 比较、再试 signed 比较（自触发要兼容无符号和有符号两种解释，这是 u4-l4 的内容）。`StInRangeLast_1` 记住上一拍的范围结果，供 Stage 2 做边沿检测。`v.Data_1 := r.Data_0;` 是纯流水搬运，把数据往后挪一级。

**Stage 2 —— 触发裁决 + 状态机 + 计数器**

[data_rec.vhd:192-294 — Stage 2：触发裁决、状态机迁移、地址与采样计数器](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L192-L294)

这是逻辑最重的一级，包含：自触发边沿检测（`StEnter_2`/`StExit_2`，第 197–198 行）、外部触发 pending（第 208–215 行）、软件触发 pending（第 218–222 行）、触发掩码合成为 `TrigNow_2`（第 225–228 行）、最小录制间隔抑制（第 231–241 行）、状态机 `case r.State_2`（第 246–276 行，这是 u3-l2 讲过的核心）、地址计数器（第 279–287 行）、采样计数器（第 290–294 行）。

注意这一级里大量读 `r.In_Vld(1)`（如第 228、256、281、292 行），正是因为「样本的有效性此时正好走到 `In_Vld(1)` 这一拍」，和 Stage 1 的 `StInRange_1`、Stage 0 的 `Data_0` 时间对齐。`v.Trigger_2 := '1';`（第 262 行）和 `v.Done(2) := '1';`（第 267 行）是状态机迁移时发出的单拍脉冲，命名带 `_2` 因为它们诞生在 Stage 2。

**Stage 3 —— 存储写端口对齐 + 计数器**

[data_rec.vhd:296-337 — Stage 3：Data_3/AdrCnt_3 流水、MemWr_3、FirstSpl_3、TrigCnt_3、DoneTime_3](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L296-L337)

这一级把 Stage 2 的结果再往后挪一拍，对齐到存储器写端口：

- `v.Data_3 := r.Data_2;`、`v.AdrCnt_3 := r.AdrCnt_2;`（第 298–299 行）——纯流水。
- `v.MemWr_3 := r.In_Vld(2);`（第 305 行）——写使能取自 `In_Vld(2)`，正好和 `Data_3` 对齐。
- `FirstSpl_3`（第 309–321 行）——在 `Trigger_2='1'` 那一拍计算首个样本地址。非二次幂深度的分支处理是 u3-l5 的内容，这里只注意它叫 `FirstSpl_3`，因为结果对齐到 Stage 3、要和存储读出配合。
- `TrigCnt_3`（第 324–328 行）、`DoneTime_3`（第 331–337 行）——两个 32 位计数器，带 `_3` 因为它们在 Stage 3 更新并最终送到输出端口。

**输出赋值**

[data_rec.vhd:339-356 — 把 record 字段连到 entity 端口](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L339-L356)

注意输出几乎全部取自带 `_3` 后缀的字段（`r.AdrCnt_3`、`r.MemWr_3`、`r.FirstSpl_3`、`r.TrigCnt_3`、`r.DoneTime_3`）或带数组下标的延迟（`r.Done(3)`），因为这些就是流水线最末端、对齐到存储器写端口的信号。`State` 输出是唯一取自 Stage 2（`r.State_2`）的输出，因为状态本身就在 Stage 2 维护，软件读状态不需要等到 Stage 3。

`Trig_Out` 单独写在架构体末尾：

[data_rec.vhd:393 — Trig_Out 直接转发 Stage 2 的 Trigger_2](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd#L393)

`Trig_Out <= r.Trigger_2;` 直接转发 Stage 2 的触发脉冲（v2.4 新增，受 `TrigForwarding_g` 控制）。

#### 4.4.4 代码实践

**实践目标**：本讲的核心实践。在源码上亲手标注每个流水级的范围，并用命名后缀解释三个典型信号为什么这么起名。

**操作步骤**：

1. 打开 [hdl/data_rec.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec.vhd)，在 `p_comb` 里用注释或纸笔圈出四个 Stage 的行号范围：
   - Stage 0：第 160–166 行（采入输入）。
   - Stage 1：第 168–188 行（数据流水 + 自触发范围判定）。
   - Stage 2：第 192–294 行（触发裁决 + 状态机 + 计数器）。
   - Stage 3：第 296–337 行（存储写端口对齐 + 计数器）。
2. 针对 `Trigger_2`、`MemWr_3`、`FirstSpl_3` 这三个信号，分别回答：
   - 它是在哪一级被赋值的？（找 `v.<信号> :=` 的行）
   - 它的命名后缀和这一级有什么关系？
3. 写下你的解释，对照下面的「预期结果」。

**需要观察的现象**：你会发现所有 `_2` 信号都在 Stage 2 段内被赋值，所有 `_3` 信号都在 Stage 3 段内被赋值，命名和代码位置严格一致。

**预期结果**（参考答案）：

- **`Trigger_2`**：在 Stage 2 的状态机里赋值（第 262 行 `v.Trigger_2 := '1';`，当 `WaitTrig_s` 命中触发时）。后缀 `_2` 表示它属于 Stage 2、代表「第 2 级裁决出的触发脉冲」。它直接驱动 `Trig_Out`（第 393 行），也驱动 Stage 3 的 `FirstSpl_3`、`TrigCnt_3`、`DoneTime_3` 计算（第 309、326、331 行）——所以 Stage 3 的这些信号必须读 `r.Trigger_2`（延迟一拍后的 Stage 2 结果），时序才能对齐。
- **`MemWr_3`**：在 Stage 3 赋值（第 302–306 行，`v.MemWr_3 := r.In_Vld(2);`）。后缀 `_3` 表示它对齐到 Stage 3，也就是存储器写端口所在的拍。它和 `r.Data_3`、`r.AdrCnt_3` 同属一级，三者一起送到 `Mem_Wr`/`Mem_Data`/`Mem_Adr`（第 349–352 行），保证「写使能、数据、地址」在同一拍对齐落盘。
- **`FirstSpl_3`**：在 Stage 3 赋值（第 309–321 行）。后缀 `_3` 表示它和存储写端口同级，便于封装层（`data_rec_vivado_wrp`）在用 AXI 读出环形缓冲时，把读地址叠加 `FirstSplAddr` 对齐成线性数据——读出逻辑和首样本地址在时间上是同一级的「约定」。

**为什么用带后缀的命名而不是都叫 `Trigger`、`MemWr`、`FirstSpl`？** 因为这个模块有 4 级流水，同名信号在不同级可能同时存在（例如 `AdrCnt_2` 和 `AdrCnt_3` 是同一个地址计数器在相邻两拍的副本）。后缀消除了歧义，让读者一眼看出「我现在读的是第几拍的值」，避免把 Stage 2 的值误当成 Stage 3 的值。

**待本地验证**：以上是源码阅读型实践，标注与解释均可直接在源码上完成，无需仿真。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `AdrCnt` 同时有 `AdrCnt_2` 和 `AdrCnt_3` 两个字段，而 `SplCnt` 只有 `SplCnt_2`？

**参考答案**：地址计数器 `AdrCnt` 需要驱动存储器写端口，而写端口对齐在 Stage 3，所以需要 `AdrCnt_3`（第 299 行 `v.AdrCnt_3 := r.AdrCnt_2;` 把 Stage 2 的地址挪到 Stage 3）。采样计数器 `SplCnt` 只在 Stage 2 的状态机里被用来判断「后触发是否采满」（第 265 行 `if r.SplCnt_2 >= unsigned(TotalSpls)`），它的结果不需要送到 Stage 3 的任何输出端口，所以不需要 `_3` 副本。

**练习 2**：如果设计者把 Stage 2 的状态机和 Stage 3 的存储写计算**合并成一级**（不做流水线），可能会出什么问题？

**参考答案**：关键路径会变长——单拍内要完成「触发裁决 → 状态机迁移 → 地址/采样计数 → 首样本地址计算（含非二次幂分支）→ 存储器写」，组合逻辑深度大，最高时钟频率会显著下降。流水线化的本质就是用「延迟换频率」：多花几拍，但每拍逻辑变浅，时序更容易收敛。

**练习 3**：`Done(2)` 和 `Done(3)` 分别在哪一级赋值？`Done` 输出端口取的是哪个？

**参考答案**：`Done(2)` 在 Stage 2 的状态机里赋值（第 267 行 `v.Done(2) := '1';`，进入 `Done_s` 时）；`Done(3)` 由流水搬运自动产生（第 158 行 `v.Done(3) := r.Done(2);`），是 `Done(2)` 延迟一拍。`Done` 输出端口取 `r.Done(3)`（第 354 行），所以输出的 Done 脉冲比状态机进入 `Done_s` 晚一拍——这是为了让 Done 脉冲和存储器写端口（Stage 3）对齐，告诉软件「数据已经稳稳落盘了」。

## 5. 综合实践

把本讲的所有知识串起来，完成下面这个「流水线追踪」任务：

**场景**：假设 `NumOfInputs_g=4`、`InputWidth_g=8`、`MemoryDepth_g=128`，记录器已 Arm 并处于 `WaitTrig_s` 状态。现在连续来 3 个有效样本 \(S_1, S_2, S_3\)（`In_Vld=1`），其中 \(S_2\) 命中触发（`TrigNow_2=1`）。

**任务**：

1. 画一张时间表，列出在触发命中后的连续 4 个时钟周期里，`r.In_Vld(0..2)`、`r.Data_0..3`、`r.Trigger_2`、`r.MemWr_3`、`r.AdrCnt_3` 分别是什么值或状态。
2. 标注 `Trigger_2='1'` 发生在哪个周期，`FirstSpl_3` 在哪个周期被计算出来。
3. 用一句话解释：为什么 `Trigger_2` 和 `FirstSpl_3` 不在同一个周期（它们差几拍、为什么）。

**提示**：

- 参考本讲 4.4.2 节的延迟对齐表，把样本换成 \(S_1, S_2, S_3\) 即可。
- `Trigger_2` 是 Stage 2 的脉冲，`FirstSpl_3` 在 Stage 3、读的是 `r.Trigger_2`（已是延迟一拍后的值）。

**预期结果**：你应该能得出「`Trigger_2` 在样本 \(S_2\) 进入后的第 2 个上升沿置 1，`FirstSpl_3` 在再后一拍（第 3 个上升沿后）被算出，两者相差 1 拍」的结论，并能解释这是因为 `FirstSpl_3` 属于 Stage 3、必须读 Stage 2 的 `Trigger_2` 才能时序对齐。

**待本地验证**：上述追踪可纯靠源码阅读完成；若想验证，可在仿真中（参考 u1-l3 的 PsiSim 流程）给 `top_tb` 加波形观察这几个信号，但本任务不要求运行仿真。

## 6. 本讲小结

- **两进程法**把时序电路拆成组合进程 `p_comb`（算下一拍 `r_next`）和时序进程 `p_seq`（在时钟沿搬入 `r <= r_next` 并同步复位），所有寄存器统一打包在 `data_rec_r` record 里。
- `p_comb` 的模板是：第一行 `v := r;`（默认保持），然后按 Stage0→3 顺序修改 `v`，末尾 `r_next <= v;`。用**变量** `v` 是为了在同一拍内顺序计算（如先算 `TrigNow_2` 再据此推进状态机）。
- 流水搬运用范围切片 `v.X(low+1 to high) := r.X(low to high-1)` 实现「整体左移」，再用 `'low`/`'high` 属性适配不同数组范围（`In_Vld` 的 `0 to 2`、`Done` 的 `2 to 3`）。
- `p_seq` 极简：`r <= r_next;` 后用 `if Rst='1'` 覆盖需要复位的字段；采用「控制信号复位、数据信号不复位」原则（如 `MemWr_3` 复位、`Data_3` 不复位）。
- 四级流水线 Stage0~3 把「采入 → 范围判定 → 触发裁决与状态机 → 存储写对齐」分开，数据与有效信号都延迟 3 拍到达存储器写端口，天然对齐。
- **命名后缀 = 流水级编号**：`Trigger_2`（Stage 2 触发脉冲）、`MemWr_3`/`FirstSpl_3`/`AdrCnt_3`（Stage 3 存储写端口对齐）、`StInRange_1`（Stage 1 范围判定）。后缀消除了同名信号跨级的歧义。

## 7. 下一步学习建议

本讲建立了 `data_rec` 的「骨架」（两进程 + 四级流水）。接下来建议：

1. **u3-l4（地址计数器、采样计数器与触发长度）**：深入 Stage 2/3 里的 `AdrCnt`、`SplCnt` 如何配合 `PreTrigSpls`/`TotalSpls` 决定录制窗口，以及环形地址回绕。
2. **u3-l5（非二次幂深度与 FirstSplAddr）**：专门讲本讲里出现的 `FirstSpl_3` 计算的 `if not NonPwr2MemDepth_c` 两个分支，以及封装层如何用 `FirstSplAddr` 把环形缓冲展开成线性数据。
3. **u4 系列（触发机制）**：把本讲 Stage 1 的自触发范围判定、Stage 2 的三种触发源裁决展开讲透（外部/软件/自触发）。
4. **u5-l2（跨时钟域）**：本讲所有信号都在数据时钟域 `Clk`；到了封装层 `data_rec_vivado_wrp`，它们要经 `status_cc`/`pulse_cc` 跨到 AXI 时钟域，届时可以回头看本讲的 `Trigger_2`、`Done` 是怎么被「搬」到另一个时钟域的。

建议在进入 u3-l4 前，先确保自己能在源码上快速指出每个 Stage 的行号范围，并能解释任意一个带后缀信号属于哪一级——这是后续阅读的基础。

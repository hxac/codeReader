# 流水线阶段与 AXI-S 握手（olo_base_pl_stage）

## 1. 本讲目标

读完本讲，你应当能够：

- 说清「为什么一个看似简单的寄存器也需要专门写成实体」——即打断长组合逻辑路径（尤其是 Ready 反压路径）的动机。
- 读懂 Open Logic 全库通用的「两进程法 + record」RTL 写法，并能照着它写自己的时序逻辑。
- 解释 AXI-S 的 Valid/Ready 握手在反压（backpressure）下如何工作，以及 `olo_base_pl_stage` 为什么需要一个「影子（shadow）寄存器」才能做到反压时不丢数据。
- 掌握 `Stages_g`（多级展开）与 `UseReady_g`（是否支持反压）两个泛型的语义与适用场景。
- 学会阅读并运行该实体的 VUnit 测试台，亲手验证反压下数据不丢失。

## 2. 前置知识

本讲假定你已经学过 **u1-l5（编码规范与阅读一个实体）** 与 **u2-l1（base 包体系）**。这里只补充几个本讲会反复用到、但前面未展开的概念。

- **时序路径与关键路径**：FPGA 中信号从某个触发器（FF）输出，经过若干组合逻辑门，到达下一个触发器输入，这段路径叫一条「时序路径」。一条路径上组合逻辑越多，传播延迟越长。延迟最长的那条叫「关键路径」，它决定了一个时钟周期能跑多快（最高时钟频率）。把一条长路径在中间插入一级寄存器「切开」，就能显著降低每段的延迟——这正是流水线寄存器的核心用途。
- **AXI-S 握手（回顾）**：数据传递用一对控制信号 `Valid`（发送方表明数据有效）和 `Ready`（接收方表明愿意接收）。一个数据真正被「传递」当且仅当在某个时钟上升沿 `Valid='1'` 且 `Ready='1'` 同时成立：
  \[
  \text{transfer}(n) \;\iff\; \text{Valid}(n)=1 \;\wedge\; \text{Ready}(n)=1
  \]
  其中 \(n\) 表示第 \(n\) 个时钟周期。
- **反压（Backpressure）**：当接收方处理不过来时，把 `Ready` 拉低，发送方就必须保持数据不动，直到 `Ready` 重新拉高。这种「下游顶住了，往回顶」的机制叫反压。
- **Ready 路径的特殊性**：在许多设计里，`Ready` 是由下游**组合地**算出来再回传给上游的。如果你在一长串流水线里让 `Ready` 一路组合传递，这条 `Ready` 路径本身就会变成关键路径。`olo_base_pl_stage` 的核心卖点之一，就是把 `Ready` 也**寄存**起来，切断这条组合链——但这会带来一个「多出来的那一拍」问题，需要 shadow 寄存器来解决（见 4.2）。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/base/vhdl/olo_base_pl_stage.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd) | 本讲主角。一个文件里定义了**两个**实体：对外公开的 `olo_base_pl_stage`（负责按 `Stages_g` 串接若干级），以及私有的 `olo_private_pl_stage_single`（真正实现「一级带反压流水线」的逻辑）。 |
| [doc/base/olo_base_pl_stage.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_pl_stage.md) | 官方文档，含接口表与架构示意图（反压/非反压两种）。 |
| [test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd) | VUnit 测试台，用 `axi_stream_master`/`axi_stream_slave` 验证组件（VC）驱动 DUT，覆盖全速、输入受限、输出受限等场景。 |
| [sim/test_configs/olo_base.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py) | 仿真配置，把 `Stages_g ∈ {0,1,5}` 与 `UseReady_g ∈ {True,False}` 笛卡尔积展开成多组测试用例。 |
| [doc/Conventions.md](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/doc/Conventions.md) | 编码规范，本讲引用其「AXI-S 握手」「复位」两节。 |

> 提示：`olo_private_pl_stage_single` 中的 `private` 前缀表示这是一个**私有实体**——库内部实现细节，不对外承诺接口稳定性，使用者只应实例化 `olo_base_pl_stage`。

---

## 4. 核心概念与源码讲解

### 4.1 两进程法与 record

#### 4.1.1 概念说明

「两进程法（two-process method）」是 Open Logic 全库时序逻辑的标准写法（u1-l5 已介绍其骨架）。它的核心思想是：把一个时序电路的状态收敛进一个 **record 类型**，然后只用两个进程描述它：

- **组合进程**（`p_comb`）：纯组合逻辑，根据「当前状态 `r`」和「输入」算出「下一拍状态 `r_next`」。它不写任何寄存器，只做计算。
- **时序进程**（`p_seq`）：只有一个职责——在时钟上升沿把 `r_next` 打入 `r`；复位时覆盖状态寄存器。

这样做的好处是：状态被 record 整齐收纳，组合/时序职责分离得干干净净，综合工具更容易推断出寄存器，人眼读起来也更接近「状态机」的直觉。`olo_base_pl_stage` 里真正干活的那一级（`olo_private_pl_stage_single`）就是教科书式的两进程法。

#### 4.1.2 核心流程

两进程法的骨架用伪代码描述如下：

```
定义 record State_r，把本实体所有需要记住的信号打包
signal r, r_next : State_r;        -- r 是当前状态，r_next 是下一拍

p_comb(all):                       -- 组合进程，敏感于所有信号
    v := r;                        -- 【关键】先把 v 抄成当前状态
    ... 根据 r 和输入，逐步修改 v ...
    r_next <= v;                   -- 把计算结果交给时序进程

p_seq(Clk):                        -- 时序进程，只敏感于时钟
    if rising_edge(Clk) then
        r <= r_next;               -- 打入新状态
        if Rst='1' then            -- 复位作为「覆盖」写在末尾
            r.<状态字段> <= <初值>;
        end if;
```

这里有两个 u1-l5 强调过的规范要点：第一，组合进程开头必须 `v := r;`（「保持变量稳定」），否则没被显式赋值的状态字段会被推断成「下一拍清空」；第二，复位以**进程末尾的覆盖**形式实现，而不是进程开头的 `if Rst` 分支——这样只有真正含状态的寄存器才接复位，复位扇出才能保持很低（见 [Conventions.md:148-150](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md#L148-L150)）。

#### 4.1.3 源码精读

私有实体 `olo_private_pl_stage_single` 的状态 record 定义如下，它把一级反压流水线需要的所有状态打包成一个类型：

```vhdl
-- two process method
type TwoProcess_r is record
    DataMain    : std_logic_vector(Width_g - 1 downto 0);
    DataMainVld : std_logic;
    DataShad    : std_logic_vector(Width_g - 1 downto 0);  -- 影子寄存器（见 4.2）
    DataShadVld : std_logic;
    In_Ready    : std_logic;                               -- 寄存后的 Ready
end record;

signal r, r_next : TwoProcess_r;
```

参见 [olo_base_pl_stage.vhd:169-177](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L169-L177) —— record 把「主寄存器」「影子寄存器」「寄存后的 In_Ready」全部收纳，`r` 与 `r_next` 成对出现，正是两进程法的典型骨架。

组合进程开头先「冻结」状态，是两进程法不可省的一步：

```vhdl
p_comb : process (all) is
    variable v         : TwoProcess_r;
    variable IsStuck_v : boolean;
begin
    -- *** Hold variables stable ***
    v := r;
```

参见 [olo_base_pl_stage.vhd:184-189](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L184-L189)。`v := r;` 之后，`p_comb` 才开始基于当前状态计算下一拍，最后 `r_next <= v;`（[第 222 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L222)）。

时序进程则极简，只管打拍 + 复位覆盖：

```vhdl
p_seq : process (Clk) is
begin
    if rising_edge(Clk) then
        r <= r_next;
        if Rst = '1' then
            r.DataMainVld <= '0';
            r.DataShadVld <= '0';
            r.In_Ready    <= '1';   -- 复位后默认能接收
        end if;
    end if;
end process;
```

参见 [olo_base_pl_stage.vhd:229-239](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L229-L239)。注意复位只覆盖了三个「状态位」（两个 Valid 和 In_Ready），`DataMain`/`DataShad` 这两个纯数据通路**不接复位**——这正是「只复位含状态的寄存器」规范（[Conventions.md:148](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/Conventions.md#L148)）的体现。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是用「两进程法」的眼光拆解 `olo_private_pl_stage_single`。

1. **实践目标**：确认 `p_comb` 只算不算、`p_seq` 只打拍，两者职责严格分离。
2. **操作步骤**：
   - 打开 [olo_base_pl_stage.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd)，定位 `g_rdy` 块内的 `p_comb`（[184-223 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L184-L223)）。
   - 逐行核对：`p_comb` 中是否出现 `rising_edge`、是否直接写 `r`（而非 `r_next`/`v`）？应该都没有。
   - 再看 `p_seq`（[229-239 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L229-L239)）：除了 `r <= r_next` 与复位覆盖，是否包含任何对输入数据的组合判断？应该也没有。
3. **需要观察的现象**：组合进程里所有对状态的修改都作用在变量 `v` 上，最终一次性 `r_next <= v`。
4. **预期结果**：你会确认 `p_comb` 是纯组合、`p_seq` 是纯时序，二者通过 `r`/`r_next` 这对信号耦合。这是写任何状态机都值得照搬的范式。
5. 待本地验证（纯阅读，无需运行）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `p_comb` 开头的 `v := r;` 删掉，会发生什么？
**答案**：`v` 会在每个周期被推断为 record 的默认值（`std_logic` 类全 `'U'`/未定义，向量全空），只有被显式赋值的字段才有效。等价于「未被赋值的状态字段每拍被清零」，电路行为完全错误。两进程法必须先 `v := r;` 锚定当前状态。

**练习 2**：为什么 `DataMain`/`DataShad` 不在复位列表里，而 `DataMainVld`/`DataShadVld` 在？
**答案**：数据通路本身不是「状态」——只要配套的 Valid 标志被复位为 0，这些数据就不会被下游当作有效数据消费，其初值无所谓。给数据寄存器也接复位只会无谓地增大复位扇出，违反「只复位含状态寄存器」的规范。

---

### 4.2 Ready 反压与 shadow 寄存器

> 这是本讲最重要、也最精妙的一节。读懂它，你就读懂了 `olo_base_pl_stage` 区别于「一个普通寄存器」的全部价值。

#### 4.2.1 概念说明

设想一个朴素的反压流水线：当 `Out_Ready=0` 时，直接把 `In_Ready` 也置 0。问题在于——如果 `In_Ready` 是**组合地**由 `Out_Ready` 推出来的，那么一长串这样的级联，会让 `Out_Ready` 一路组合回传到最上游，形成一条很长的 `Ready` 组合链，成为关键路径。

`olo_base_pl_stage` 的做法是：**把 `In_Ready` 也寄存一拍**。这样每级的 `Ready` 都由本级的触发器直接驱动，组合链被切断，时序非常好。

但寄存 `Ready` 引入了一个「时序错位」：当下游在第 \(n\) 拍把 `Out_Ready` 拉低时，本级的 `In_Ready` 要等到第 \(n+1\) 拍才能跟着变低（因为它被寄存了）。而在第 \(n\) 拍这个「空档」里，`In_Ready` 仍然是 1，上游**还会再塞进来一个数据**。如果此时本级的主寄存器（输出寄存器）已经满了，这个多出来的数据就会丢失。

解决办法就是 **shadow（影子）寄存器**：再留一个寄存器，专门用来「吸收」这个因 `Ready` 寄存延迟而多出来的那一个数据。官方文档因此把它准确地描述为：**带反压的一级，实质上是一个「双-entry FIFO」**（[olo_base_pl_stage.md:76-78](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_pl_stage.md#L76-L78)）。

#### 4.2.2 核心流程

一级反压流水线内部有「主寄存器（DataMain）」和「影子寄存器（DataShad）」两个数据槽，外加一个寄存后的 `In_Ready`。定义「卡住（stuck）」状态：

\[
\text{IsStuck} \;\iff\; \underbrace{\text{DataMainVld}=1}_{\text{主寄存器满}} \;\wedge\; \underbrace{\text{Out\_Ready}=0}_{\text{下游不收}} \;\wedge\; \underbrace{(\text{In\_Valid}=1 \;\vee\; \text{DataShadVld}=1)}_{\text{有数据正在来/已在影子中}}
\]

每个周期组合进程按固定顺序做三件事：

```
1) 结算输出：若主寄存器有效且下游就绪 (DataMainVld=1 & Out_Ready=1)，
            则把影子寄存器内容提升到主寄存器，清空影子。
            —— 下游一腾出位置，影子里的数据就「补位」到输出。

2) 接收输入：若发生握手 (In_Ready=1 & In_Valid=1)，
            - 处于 IsStuck：数据存入「影子寄存器」（因为主寄存器还满着）；
            - 否则正常：数据直接进「主寄存器」。

3) 维护 In_Ready：若 IsStuck，则下一拍 In_Ready=0（开始顶住上游）；否则 In_Ready=1。
```

一个反压触发的典型时序（设初态为空、下游先就绪）：

| 周期 | 事件 | In_Ready（本拍） | 主寄存器 | 影子寄存器 | 说明 |
| :--: | :--- | :--: | :--- | :--- | :--- |
| A | D0 到达，下游就绪 | 1 | D0 | 空 | IsStuck=0，D0 直入主寄存器 |
| B | D1 到达，**下游突然拉低 Out_Ready** | 1（仍为旧值） | D0 | **D1** | IsStuck=1：主满且下游不收，D1 只能进影子；同时下一拍 In_Ready 置 0 |
| C | 上游被 In_Ready=0 顶住，不送数据 | 0 | D0 | D1 | 下游仍不收则保持；下游一旦拉高 Out_Ready，D0 被消费，D1 提升到主寄存器 |

关键在第 B 拍：正是 shadow 寄存器把 D1 救了下来。如果没有它，D1 就丢了。这就是「反压时不丢数据」的全部秘密。

#### 4.2.3 源码精读

`IsStuck` 的判定一行写完，是整段逻辑的「题眼」：

```vhdl
-- *** Simplification Variables ***
IsStuck_v := (r.DataMainVld = '1' and Out_Ready = '0' and (In_Valid = '1' or r.DataShadVld = '1'));
```

参见 [olo_base_pl_stage.vhd:191-192](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L191-L192)。

「结算输出」——下游腾位时，影子提升为主：

```vhdl
-- *** Handle output transactions ***
if r.DataMainVld = '1' and Out_Ready = '1' then
    v.DataMainVld := r.DataShadVld;
    v.DataMain    := r.DataShad;
    v.DataShadVld := '0';
end if;
```

参见 [olo_base_pl_stage.vhd:194-199](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L194-L199)。

「接收输入」——握手时根据是否 IsStuck 决定数据进主还是进影子，这就是 shadow 生效的地方：

```vhdl
-- *** Latch incoming data ***
if r.In_Ready = '1' and In_Valid = '1' then
    -- If we are stuck, save data in shadow register because ready is deasserted only after one clock cycle
    if IsStuck_v then
        v.DataShadVld := '1';
        v.DataShad    := In_Data;
    -- In normal case, forward data directly to the output registers
    else
        v.DataMainVld := '1';
        v.DataMain    := In_Data;
    end if;
end if;
```

参见 [olo_base_pl_stage.vhd:201-212](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L201-L212)。注释直接点明动机：Ready 是「延迟一拍」才被撤销的，所以这一拍多出来的数据必须先存进影子。

「维护 In_Ready」——卡住时拉低，否则拉高：

```vhdl
-- *** Remove Rdy if stuck ***
if IsStuck_v then
    v.In_Ready := '0';
else
    v.In_Ready := '1';
end if;
```

参见 [olo_base_pl_stage.vhd:214-219](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L214-L219)。

最后，组合进程把 record 的字段接到对外端口上（注意 `In_Ready` 取的是寄存后的 `r.In_Ready`，从而切断了组合反压链）：

```vhdl
In_Ready  <= r.In_Ready;
Out_Valid <= r.DataMainVld;
Out_Data  <= r.DataMain;
```

参见 [olo_base_pl_stage.vhd:225-227](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L225-L227)。

#### 4.2.4 代码实践

这是一个**仿真观察型实践**，目标是亲眼看到 shadow 寄存器生效的瞬间。

1. **实践目标**：构造「主寄存器已满、下游突然反压、上游仍送一个数据」的场景，观察该数据进入影子寄存器而非丢失。
2. **操作步骤**：
   - 复用本讲自带的测试台 [olo_base_pl_stage_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd)。它已用 VUnit 的 `axi_stream_master`/`axi_stream_slave` VC 驱动 DUT（[第 56-63 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd#L56-L63) 定义了带随机 stall 的主/从 VC）。
   - 在 `sim/` 下运行反压用例，例如：
     ```
     cd sim
     python run.py -v --ghdl -p "*OutLimited*"
     ```
     该用例（[第 140-147 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd#L140-L147)）让从机每 5 拍才就绪一次（`OutDelay_v := Clk_Period_c*5`），制造持续反压，并连发 100 个值（`push100`），再用 `check100` 逐一核对（[第 66-86 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd#L66-L86)）。
   - 若想看波形，可改用带波形输出的仿真器（如 `--nvc --gtkwave` 或 GHDL 的 `--vcd`），把 `In_Valid`、`In_Ready`、`In_Data`、`Out_Valid`、`Out_Ready`、`Out_Data`，以及内部信号 `r.DataMainVld`、`r.DataShadVld`、`r.DataShad` 加入波形窗口。
3. **需要观察的现象**：在某个 `Out_Ready` 由 1 变 0 的周期附近，会看到 `In_Ready` 仍维持 1 一拍，同时 `r.DataShadVld` 被置 1——这正是 D1 落入 shadow 的时刻；随后 `Out_Ready` 拉高时 `r.DataShadVld` 清 0，对应影子数据提升为主寄存器输出。
4. **预期结果**：100 个数据全数正确按序到达输出端，无一丢失（`check100` 全部通过）。这正面证明了 shadow 机制在反压下保住了数据。
5. 若本地无 GHDL/NVC 环境，命令部分标注「待本地验证」，但上述源码级时序分析（4.2.2 的表格）可作为理论依据先行确认。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `In_Ready <= r.In_Ready;` 改成组合输出 `In_Ready <= (not IsStuck_internal);`（即不寄存 Ready），电路还能正常工作吗？还会有 shadow 的需要吗？
**答案**：功能上可能仍正确（反压能即时回传），但这就丧失了 `olo_base_pl_stage` 切断 `Ready` 组合链的核心价值——长级联时 `Ready` 路径又变回关键路径。同时，若 Ready 不寄存，就不存在「多出来的一拍空档」，自然也不需要 shadow。可见 **shadow 是「寄存 Ready」这一选择的直接代价/配套措施**。

**练习 2**：shadow 寄存器最多同时存几个数据？为什么够用？
**答案**：最多 1 个。因为 Ready 只被「延迟一拍」，所以因延迟而多塞进来的数据至多一个；一个影子寄存器恰好吸收它。再多就是逻辑设计错误了。

---

### 4.3 Stages_g 多级展开

#### 4.3.1 概念说明

`olo_base_pl_stage` 并不是把 N 级逻辑写 N 遍，而是用「顶层实体 + generate 循环」把私有单级实体 `olo_private_pl_stage_single` 串成一条链。`Stages_g` 控制串联的级数：

- `Stages_g=1`（默认）：最常用，用来把一条过长的组合路径在中间切一刀。
- `Stages_g>1`：用来给长布线（routing）路径插多级寄存器，常见于跨芯片区域、跨 die 的连接。
- `Stages_g=0`：特殊情形，实体退化为「纯直通」，连一拍延迟都没有——只是为了在代码里保持接口统一。

这种「一个泛型控制整条链」的设计，让使用者在不改 RTL 的前提下灵活调节时序裕量。

#### 4.3.2 核心流程

顶层架构用数组信号把若干级首尾相连。关键数据结构是三个**长度为 `Stages_g+1`** 的数组，分别承载各级之间的数据、有效、就绪：

```
Data  : 0 .. Stages_g     -- 各级间的数据（共 Stages_g+1 段）
Valid : 0 .. Stages_g
Ready : 0 .. Stages_g

Data(0)  <- In_Data       ;  Valid(0)  <- In_Valid  ;  In_Ready <- Ready(0)
第 i 级 (i=0..Stages_g-1):  single_stage( Data(i),Valid(i),Ready(i) -> Data(i+1),Valid(i+1),Ready(i+1) )
Data(Stages_g) -> Out_Data; Valid(Stages_g) -> Out_Valid; Ready(Stages_g) <- Out_Ready
```

注意 `Ready` 是**反方向**流动的（从下游 `Out_Ready` 一路往上传到 `In_Ready`），而 `Data`/`Valid` 是正方向。这与 4.2 一致：每级内部的 shadow 保证反压回传时不丢数据。

#### 4.3.3 源码精读

顶层数组信号定义（注意范围是 `0 to Stages_g`，比级数多 1，用来表示「级与级之间」的段）：

```vhdl
type Data_t is array (natural range <>) of std_logic_vector(Width_g - 1 downto 0);

signal Data  : Data_t(0 to Stages_g);
signal Valid : std_logic_vector(0 to Stages_g);
signal Ready : std_logic_vector(0 to Stages_g);
```

参见 [olo_base_pl_stage.vhd:78-83](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L78-L83)。

多级串接的核心：一个 `for ... generate` 循环，每次例化一个单级实体，把第 `i` 段连到第 `i+1` 段：

```vhdl
g_stages : for i in 0 to Stages_g - 1 generate
    i_stg : component olo_private_pl_stage_single
        generic map ( Width_g => Width_g, UseReady_g => UseReady_g )
        port map (
            Clk => Clk, Rst => Rst,
            In_Valid  => Valid(i),   In_Ready  => Ready(i),   In_Data  => Data(i),
            Out_Valid => Valid(i+1), Out_Ready => Ready(i+1), Out_Data => Data(i+1)
        );
end generate;
```

参见 [olo_base_pl_stage.vhd:93-112](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L93-L112)。外围把对外端口接到数组两端（[第 89-91 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L89-L91) 接输入端，[第 114-116 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L114-L116) 接输出端）。

`Stages_g=0` 时走另一个 generate 分支，纯直通、零延迟：

```vhdl
g_zero : if Stages_g = 0 generate
    Out_Valid <= In_Valid;
    Out_Data  <= In_Data;
    In_Ready  <= Out_Ready;
end generate;
```

参见 [olo_base_pl_stage.vhd:119-124](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L119-L124)。两个互斥的 `generate`（`g_nonzero` 与 `g_zero`）由同一个条件 `Stages_g > 0` 与 `Stages_g = 0` 区分，保证任意 `Stages_g` 取值都有且只有一条路径被综合。

#### 4.3.4 代码实践

1. **实践目标**：直观感受 `Stages_g` 对延迟的影响——级数越多，数据从输入到输出的「飞拍」越多。
2. **操作步骤**：
   - 看仿真配置 [olo_base.py:125-131](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L125-L131)，它对 `Stages ∈ {0,1,5}` 各跑一遍。
   - 分别运行 `Stages_g=0` 与 `Stages_g=5` 的 `Basic` 用例（单数据收发，[第 125-133 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd#L125-L133)），在波形上量从 `In_Valid&In_Ready` 同时拉高，到 `Out_Valid` 拉高的时钟周期数。
3. **需要观察的现象**：`Stages_g=0` 时输出与输入几乎同拍（直通）；`Stages_g=5` 时约延迟 5 拍。
4. **预期结果**：延迟拍数 ≈ `Stages_g`（`UseReady_g=false` 时严格相等；`UseReady_g=true` 在稳态无反压时也接近 `Stages_g`，因为数据每级走「直入主寄存器」的快路径，shadow 只在反压时才介入）。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么数组信号范围是 `0 to Stages_g` 而不是 `0 to Stages_g-1`？
**答案**：N 级串联需要 N+1 个「段」来表示级与级之间的连线（就像 3 节车厢之间有 4 个车钩位）。第 `i` 级吃第 `i` 段、吐第 `i+1` 段，所以需要 `Stages_g+1` 个元素。

**练习 2**：`Stages_g=0` 时还有没有 shadow？为什么直通分支里 `In_Ready <= Out_Ready` 是安全的？
**答案**：没有 shadow——因为根本没有寄存器，也就没有「寄存 Ready 带来的多一拍空档」，不需要吸收任何数据。直通把 `Out_Ready` 原样回传，反压即时生效，不会丢数据。

---

### 4.4 UseReady_g 无反压模式

#### 4.4.1 概念说明

并非所有数据通路都需要反压。例如：数据源是按固定节拍产生的传感器采样流，永远不会有「下游顶住」的情况。此时再额外维护 shadow 寄存器、寄存 Ready，纯属浪费面积。

`UseReady_g` 就是这个开关：

- `UseReady_g=true`（默认）：上一节那套带 shadow 的反压流水线。
- `UseReady_g=false`：最朴素的一级（或多级）寄存器，`In_Ready` 恒为 1，**不处理反压**。因为逻辑极简，综合工具会倾向于把这些寄存器合并进移位寄存器原语（如 AMD 的 SRL）或与其他寄存器合并——这会破坏「每级都用独立 FF、时序可控」的初衷。因此该分支特意加了若干**综合属性**，强制把它们实现为普通触发器。

#### 4.4.2 核心流程

无反压分支只需要一个进程：把输入数据和有效标志各打一拍，复位时只清有效标志。

```
process(Clk):
    if rising_edge(Clk):
        DataReg <= In_Data        -- 数据打一拍
        VldReg  <= In_Valid       -- 有效打一拍
        if Rst: VldReg <= '0'
In_Ready <= '1'                   -- 永远声称能收（不反压）
Out_Data  <= DataReg
Out_Valid <= VldReg
```

围绕 `DataReg`/`VldReg` 挂了一组综合属性，目的统一：**别把这对寄存器优化掉/合并掉/塞进 SRL，给我老老实实做成 FF**。

#### 4.4.3 源码精读

无反压分支的核心进程：

```vhdl
p_stg : process (Clk) is
begin
    if rising_edge(Clk) then
        DataReg <= In_Data;
        VldReg  <= In_Valid;
        if Rst = '1' then
            VldReg <= '0';
        end if;
    end if;
end process;

In_Ready  <= '1'; -- Not used!
Out_Data  <= DataReg;
Out_Valid <= VldReg;
```

参见 [olo_base_pl_stage.vhd:270-283](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L270-L283)。注释 `-- Not used!` 明示 `In_Ready` 只是为了接口一致而存在，恒为 1。

综合属性块——这些常量全部来自 base 包 `olo_base_pkg_attribute`（见 u2-l1），是跨厂商可综合的关键：

```vhdl
-- Synthesis attributes - suppress shift register extraction
attribute shreg_extract of VldReg  : signal is ShregExtract_SuppressExtraction_c;  -- "no"
attribute syn_srlstyle of VldReg   : signal is SynSrlstyle_FlipFlops_c;            -- "registers"
-- Synthesis attributes - preserve registers
attribute dont_merge of VldReg     : signal is DontMerge_SuppressChanges_c;        -- true
attribute preserve   of VldReg     : signal is Preserve_SuppressChanges_c;
attribute syn_keep    of VldReg    : signal is SynKeep_SuppressChanges_c;
attribute syn_preserve of VldReg   : signal is SynPreserve_SuppressChanges_c;
```

参见 [olo_base_pl_stage.vhd:248-266](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pl_stage.vhd#L248-L266)（`DataReg` 亦同）。这些属性的取值定义在 [olo_base_pkg_attribute.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_attribute.vhd) 中，例如 `ShregExtract_SuppressExtraction_c = "no"`（[第 35 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_attribute.vhd#L35)）、`SynSrlstyle_FlipFlops_c = "registers"`（[第 50 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_attribute.vhd#L50)）、`DontMerge_SuppressChanges_c = true`（[第 65 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_pkg_attribute.vhd#L65)）。一句概括：阻止移位寄存器提取（`shreg_extract`/`syn_srlstyle`）+ 阻止寄存器合并/优化（`dont_merge`/`preserve`/`syn_keep`/`syn_preserve`）。

> 文档 [olo_base_pl_stage.md:66-68](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/base/olo_base_pl_stage.md#L66-L68) 也明确说明：这些属性「保证寄存器被实现为 FF，不会被合并进移位寄存器」。

#### 4.4.4 代码实践

1. **实践目标**：对比 `UseReady_g=true` 与 `false` 两种模式在反压下的行为差异。
2. **操作步骤**：
   - 仿真配置对两种取值都跑了测试（[olo_base.py:128-131](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L128-L131)）。
   - 注意测试台里 `OutLimited` 用例有一行保护：`if UseReady_g then ... end if;`（[第 140-147 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd#L140-L147)）。也就是说，`UseReady_g=false` 时**不测**反压——因为该模式根本不支持反压，从机 stall 的数据会丢，这是设计预期而非 bug。
3. **需要观察的现象**：在 `UseReady_g=false` 的 `FullThrottle` 用例（从机不 stall，[第 135-138 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd#L135-L138)）下数据正常通过；而若强行给它加反压，会观察到丢数。
4. **预期结果**：理解 `UseReady_g` 是一个「能力换面积」的取舍——用反压能力换取更小的面积（无 shadow、无 Ready 寄存器）。
5. 待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么无反压分支要加这么多综合属性？不加会怎样？
**答案**：这对寄存器（数据 + 有效）逻辑极简，综合工具会倾向于把它实现为移位寄存器原语（SRL，用 LUT 当移位寄存器以省 FF），或与其他寄存器合并。这会让「每一级都是独立 FF、时序可预测」的设计意图落空。属性强制它做成独立 FF。

**练习 2**：`UseReady_g=false` 时，把一个下游会 stall 的模块直接接到 `Out_Ready` 上安全吗？
**答案**：不安全。该模式下 `In_Ready` 恒为 1，上游会无脑每拍送数；一旦下游 stall，本级没有 shadow 也没有任何缓冲，必然丢数据。该模式只适用于「下游保证不反压」的场景。

---

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个贯穿性任务。

**任务**：实例化一个 `Stages_g=2`、`Width_g=16`、`UseReady_g=true` 的 `olo_base_pl_stage`，验证在 `Out_Ready` 被拉低（反压）时数据不丢失，并用波形定位 shadow 寄存器生效的瞬间。

**建议做法**：

1. **复用现成测试台，避免从零搭**。本讲自带的 [olo_base_pl_stage_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd) 已经是一个参数化、带反压随机 stall 的完整 TB：
   - DUT 例化见 [第 173-188 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd#L173-L188)，`Stages_g`/`UseReady_g` 由 generic 传入。
   - 主/从 VC 见 [第 193-213 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd#L193-L213)，从机 stall 概率在 [第 60-63 行](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd#L60-L63) 配置。
2. **选择目标配置运行**。`Stages_g=2` 不在默认配置 `{0,1,5}` 里，你需要在 [olo_base.py 的 pl_stage 段](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_base.py#L125-L131) 临时把 `5` 改成 `2`（或新增一行 `named_config`），然后：
   ```
   cd sim
   python run.py -v --ghdl -p "*olo_base_pl_stage*OutLimited*"
   ```
3. **打开波形，定位 shadow 生效时刻**。把 TB 信号 + DUT 内部 `r.DataMainVld`、`r.DataShadVld`、`r.DataShad` 都拉进波形（必要时把 DUT 的 `r` 信号加到波形/在 TB 里引出）。
   - 找一个 `Out_Ready` 由 1→0 的边沿；
   - 确认其后约 1 拍内 `r.DataShadVld` 出现一个脉冲（数据落入影子）；
   - 确认 `Out_Ready` 再次拉高后，影子内容被「提升」到输出、`r.DataShadVld` 归零。
4. **核对不丢数据**。`OutLimited` 用例连发 100 个递增值并由 `check100` 逐一校验（[第 77-86 行](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/test/base/olo_base_pl_stage/olo_base_pl_stage_tb.vhd#L77-L86)）。用例全部通过即等价于「反压下 0 丢失」。

**预期结果**：仿真全绿；波形上能清晰指出至少一处 `r.DataShadVld` 置位的时刻，并把它和 4.2.2 时序表中的「周期 B」对应起来。若本地无仿真环境，4.2.2 的时序分析与源码注释已构成充分的理论证明，命令运行部分标注「待本地验证」。

## 6. 本讲小结

- `olo_base_pl_stage` 不是「一个寄存器」那么简单：它的核心价值是**切断长组合路径**，尤其是常被异步回传、容易成为关键路径的 **Ready 反压路径**。
- 它用**两进程法 + record** 组织代码：`p_comb` 只算、`p_seq` 只打拍，状态全收进 record，复位以进程末尾覆盖实现，只复位状态位。
- 为了把 `Ready` 寄存一拍而不丢数据，每级带反压的实现实质是一个**双-entry FIFO**：主寄存器 + shadow 寄存器。`IsStuck` 判定 + 三步组合逻辑（结算输出 / 接收输入 / 维护 Ready）共同保证反压下数据零丢失。
- `Stages_g` 用 `for generate` 把单级实体串成链，数组信号范围 `0 to Stages_g`；`Stages_g=0` 走单独 generate 退化为纯直通。
- `UseReady_g=false` 关闭反压，退化为最简寄存器并用一组跨厂商综合属性强制实现为独立 FF，换取更小面积，代价是**不能接会反压的下游**。

## 7. 下一步学习建议

- **横向对比 AXI 版本**：下一站可读 `olo_axi_pl_stage`（u6-l1），它把同样的「寄存 + 反压」思路扩展到完整 AXI4/AXI4-Lite 接口（5 个通道），你会看到本讲的 record 与 generate 套路如何被放大到总线级。
- **纵向进入 FIFO**：本讲的「双-entry FIFO」隐喻，建议接着学真正的同步 FIFO `olo_base_fifo_sync`（u2-l4）与异步 FIFO `olo_base_fifo_async`（u3-l1），它们把「反压 + 缓冲」做到任意深度，并引入几乎满/几乎空等级。
- **吃透反压的下游用法**：留意后续讲义中凡是出现长组合链或跨 die 连接的地方，几乎都会用 `olo_base_pl_stage` 来「切路径」——把本讲当作全库的通用零件来理解。

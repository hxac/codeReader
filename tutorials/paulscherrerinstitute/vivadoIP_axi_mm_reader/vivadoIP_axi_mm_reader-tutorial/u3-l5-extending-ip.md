# 二次开发实践：扩展该 IP

## 1. 本讲目标

学完本讲，你应当能够：

- 把「给 `vivadoIP_axi_mm_reader` 新增一个寄存器」这件事拆成一条**端到端改动链**，并说清楚链路上每一处文件各自负责什么、为什么必须同步修改。
- 读懂 `definitions_pkg.vhd` 里那一组 `RegIdx_*` 常量是整张寄存器地图的**唯一事实来源**，并利用 `USER_SLV_NUM_REG = 2**log2ceil(RegCount_c)` 这一「向上取整到 2 的幂」的性质，判断一次扩展是「零地址位移的低风险改动」还是「会撬动整张内存映射的高风险改动」。
- 在 `tb/top_tb.vhd` 的自校验测试台里，用现有的 `StimCase`/`RespCase` 握手机制规划一个新断言或新用例，并确认 `sim/config.tcl` 在「不新增源文件」的前提下**无需修改**。
- 同步更新 C 驱动（`drivers/axi_mm_reader/src/*.h/*.c`）与文档（`doc/Documentation.md`），让软件侧与 RTL 侧的寄存器偏移严格对齐。
- 具备「安全扩展 IP（含仿真验证）」的工程能力：知道哪些改动可以孤立完成、哪些改动会级联扩散到测试、驱动、文档甚至 GUI。

## 2. 前置知识

本讲是全手册的收尾篇，不再引入新的 RTL 机制，而是把前几讲的知识串成一条「改一个东西要动几个文件」的工程流程。阅读本讲前，你最好已经建立以下认知（本讲会简要回顾关键点并给出链接）：

- **寄存器地图**：IP 经 `s00_axi`（AXI 从机）把配置/状态铺成一段连续地址空间——前段是 5 个固定寄存器（`Ena`/`RegCnt`/`RdData`/`RdLast`/`Level`），后段从字节 `0x20` 起是 `Addr[]` 配置表（RegTable）。所有地址以可读名字集中定义在 `definitions_pkg.vhd`。详见 [u2-l2](u2-l2-register-map.md)。
- **核心 + wrapper 分层**：`axi_mm_reader.vhd` 是纯逻辑核心（只懂简化的 IPIC 接口），`axi_mm_reader_wrp.vhd` 才是真正的 AXI4 接口边界，`definitions_pkg.vhd` 提供共享常量。**改读周期逻辑只动核心，改 AXI 行为只动 wrapper，改地址名字三者都可能动。** 详见 [u2-l3](u2-l3-core-fsm.md)、[u2-l5](u2-l5-axi-slave-wrapper.md)。
- **RV（带副作用读）**：读 `RdData` 会弹 FIFO，读 `RdLast` 仅 peek 不弹，故 AXIMM 取数必须**先读 `RdLast`、再读 `RdData`**。新增寄存器时若也涉及副作用，必须同样在驱动与文档里写清楚读序。详见 [u2-l2](u2-l2-register-map.md)、[u3-l1](u3-l1-c-driver.md)。
- **自校验测试台**：`tb/top_tb.vhd` 用 `p_control`（发激励与校验）和 `p_spi`（扮演 `m00_axi` 从机回送数据）两个进程，靠 `StimCase`/`RespCase` 一对整数信号做阻塞握手，把回归切成 6 个用例；`sim/config.tcl` 再用 `OutputType_g=AXIS`/`AXIMM` 两个 generic 让同一测试台跑两遍。详见 [u3-l2](u3-l2-testbench.md)。
- **C 驱动**：每个 API 即「按地址常量算字节地址 + 一次 `Xil_In32`/`Xil_Out32`」，地址常量与 RTL 的字索引严格对应（字节地址 = 字索引 × 4）。详见 [u3-l1](u3-l1-c-driver.md)。

> 一句话定位：本讲不教你「写什么新功能」，而是教你「无论想加什么功能，都要沿着 `definitions_pkg → 核心 → wrapper → top_tb → 驱动 → 文档` 这条链路把改动走完，并且能判断每一步的风险等级」。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用来讲什么 |
| --- | --- | --- |
| `hdl/definitions_pkg.vhd` | 寄存器地址常量包 | 整张寄存器地图的**唯一事实来源**；新增寄存器最先改这里，并据此判断地址是否位移 |
| `hdl/axi_mm_reader.vhd` | 纯逻辑核心（FSM） | 内部状态（如 `DoneCnt`）目前不对外暴露；扩展「读内部状态」类寄存器时需要在这里加输出端口 |
| `hdl/axi_mm_reader_wrp.vhd` | AXI4 wrapper | 把核心端口接到 IPIC 的 `reg_rdata` 数组对应槽位；`USER_SLV_NUM_REG` 的向上取整决定了「空闲槽位」 |
| `tb/top_tb.vhd` | 自校验测试台 | 用 `StimCase`/`RespCase` 握手机制为新增行为加断言或新用例 |
| `sim/config.tcl` | 仿真源文件分组 | 确认「不新增源文件」时无需改动；理解何时必须改它 |
| `drivers/axi_mm_reader/src/axi_mm_reader.h` | C 驱动头文件 | 地址宏与错误码；新增寄存器要加对应 `#define` 与函数声明 |
| `drivers/axi_mm_reader/src/axi_mm_reader.c` | C 驱动实现 | 以 `MmReader_GetLevel` 为模板写一个只读 getter |
| `doc/Documentation.md` | IP 文档 | 寄存器表新增一行，软件使用者据此编程 |

## 4. 核心概念与源码讲解

在进入三个最小模块之前，先用一张「改动影响总览表」建立全局心智模型。下表列出**新增一个寄存器**时，每个文件「是否要改、改什么、风险点」：

| 文件 | 是否要改 | 关键改动 | 风险点 |
| --- | --- | --- | --- |
| `hdl/definitions_pkg.vhd` | **必改** | 加 `RegIdx_X_c` 常量；更新 `RegCount_c` | 若新 `RegCount_c` 跨过 2 的幂边界，会撬动 `USER_SLV_NUM_REG` 与内存映射 |
| `hdl/axi_mm_reader.vhd` | 视情况 | 若寄存器反映核心内部量（如 `DoneCnt`），加输出端口与赋值 | 端口加在核心、接线加在 wrapper，两处须一致 |
| `hdl/axi_mm_reader_wrp.vhd` | **必改** | 把新值驱动到 `reg_rdata(RegIdx_X_c)` | 只读寄存器驱动放在 generate 块外（两种输出模式共享） |
| `tb/top_tb.vhd` | **必改** | 为新行为加断言或新 `StimCase` | 新用例要在 `p_control` 与 `p_spi` 两端同步握手 |
| `sim/config.tcl` | 通常**不改** | 仅当新增了 `.vhd` 源文件才需在此登记 | 误改 generic 顺序会破坏 AXIS/AXIMM 双跑 |
| `drivers/.../axi_mm_reader.h` | **必改** | 加 `#define MM_READER_X_REG` 与函数声明 | 字节地址 = 字索引 × 4，必须与 RTL 一致 |
| `drivers/.../axi_mm_reader.c` | **必改** | 实现只读 getter（套 `GetLevel` 模板） | 若寄存器有读副作用，须遵守读序 |
| `doc/Documentation.md` | **必改** | 寄存器表加一行 | 地址、模式（R/W/RW/RV）、位域须与实现一致 |

记住这张表，下面三个模块就是在解释「为什么是这样」并给出可操作的样例。

### 4.1 新增寄存器流程

#### 4.1.1 概念说明

「新增一个寄存器」在这个 IP 里之所以是一条**跨文件链路**，根本原因是：地址信息被故意集中在 `definitions_pkg.vhd` 这一个包里，再被核心、wrapper、测试台、C 驱动**各自引用**。这是一把双刃剑——好处是改一处名字、处处生效；代价是任何新增都必须沿着引用链把每一处补齐，漏掉任何一处都会导致「RTL 能跑、但软件读不到」或「仿真正确、但驱动写错地址」的隐蔽不一致。

本模块围绕一个具体且贴近真实需求的扩展展开：**把核心内部当前已收到的数据字数 `DoneCnt` 暴露成一个只读状态寄存器**，让软件能在读周期进行中查询进度。当前 `DoneCnt` 纯粹是核心 record 里的一个内部字段，外部完全看不到——这正是「读取内部状态」类扩展的典型场景。

这里有一个**极其关键的工程洞察**，也是本讲最重要的结论之一：IPIC 从机为寄存器区分配的槽数 `USER_SLV_NUM_REG` 是**向上取整到 2 的幂**的，而当前实际只用了其中一部分，存在「空闲槽位」。这意味着小规模扩展可以做到**零地址位移**，而一旦跨过 2 的幂边界，整张内存映射就会整体后移，级联影响测试、驱动、文档。这个判断在动手前必须先做。

#### 4.1.2 核心流程

新增一个只读状态寄存器（以「暴露 `DoneCnt`」为例）的标准流程：

1. **定索引**：在 `definitions_pkg.vhd` 给新寄存器分配一个 `RegIdx_X_c` 常量，并更新 `RegCount_c`。
2. **判风险**：用 `USER_SLV_NUM_REG = 2**log2ceil(RegCount_c)` 算出新寄存器区槽数，判断是否跨过 2 的幂边界（决定是否位移内存映射）。
3. **取数据**：若新寄存器反映核心内部量，在 `axi_mm_reader.vhd` 加一个输出端口把内部信号引出来。
4. **接线**：在 `axi_mm_reader_wrp.vhd` 把该端口驱动到 `reg_rdata(RegIdx_X_c)` 这个槽位。
5. **写测试**：在 `top_tb.vhd` 为新寄存器加断言（见 4.2）。
6. **配仿真**：确认 `sim/config.tcl` 是否需要改（通常不需要，见 4.2）。
7. **更驱动**：在 C 驱动加地址宏与 getter（见 4.3）。
8. **更文档**：在 `Documentation.md` 寄存器表加一行（见 4.3）。

其中第 2 步是「安全扩展」的灵魂，下面用源码精读把它讲透。

#### 4.1.3 源码精读

**(a) 寄存器地图的唯一事实来源**

[`hdl/definitions_pkg.vhd:24-35`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/definitions_pkg.vhd#L24-L35) —— 所有寄存器地址以**字索引**（word index）形式集中定义在这里。注意几个关键点：`RegIdx_Level_c = 4` 是当前最大索引；`RegCount_c := RegIdx_Level_c+1` 派生出寄存器总数（当前 = 5）；`MemOffs_c := 8` 是内存区（RegTable）起始的字索引常量。

```vhdl
constant RegIdx_Ctrl_c   : natural := 0;
constant RegIdx_RegCnt_c : natural := 1;
constant RegIdx_RdData_c : natural := 2;
constant RegIdx_RdLast_c : natural := 3;
constant RegIdx_Level_c  : natural := 4;
constant RegCount_c      : natural := RegIdx_Level_c+1;   -- = 5
constant MemOffs_c       : natural := 8;
```

> 这里存的是**字索引**，软件用的是字节地址（字索引 × 4）。例如 `RegIdx_Level_c=4` → 字节 `0x10`，与文档和 C 宏一致。

**(b) 向上取整到 2 的幂——空闲槽位的来源**

[`hdl/axi_mm_reader_wrp.vhd:133`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L133) —— wrapper 用这一行把寄存器区槽数向上取整到 2 的幂：

```vhdl
constant USER_SLV_NUM_REG : integer := 2**log2ceil(RegCount_c);
```

当前 `RegCount_c = 5`，`log2ceil(5) = 3`（因为 \(2^2=4 < 5 \le 2^3=8\)），故 `USER_SLV_NUM_REG = 2**3 = 8`。也就是说，IPIC 从机为寄存器区准备了 **8 个槽位**，但实际只用了索引 0–4 共 5 个，**索引 5、6、7 是已分配但未使用的空闲槽位**。

这带来一条极其重要的结论：

\[ \text{USER\_SLV\_NUM\_REG} = 2^{\lceil \log_2 \text{RegCount\_c} \rceil} \]

- 当 `RegCount_c` 从 5 增长到 6、7、8 时，\(\lceil \log_2 \text{RegCount\_c} \rceil\) 仍等于 3，`USER_SLV_NUM_REG` 仍是 8 —— **内存映射零位移**。
- 当 `RegCount_c` 增长到 9 时，\(\lceil \log_2 9 \rceil = 4\)，`USER_SLV_NUM_REG` 跳到 16 —— **RegTable 内存区整体后移**。

这就是本讲的核心风险判据：**新增 1～3 个寄存器（填满到索引 7）是低风险改动；新增第 4 个寄存器（使 `RegCount_c` 达到 9）会触发地址位移，级联影响下文 4.2、4.3 的多处常量。**

**(c) 内存区位移会破坏哪些硬编码常量**

内存区（RegTable）在 AXI 地址空间里的起点 = `USER_SLV_NUM_REG × 4` 字节。当前 = `8 × 4 = 0x20`。注意 `MemOffs_c` 是**硬编码的 8**，并非由 `USER_SLV_NUM_REG` 派生：

[`hdl/definitions_pkg.vhd:35`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/definitions_pkg.vhd#L35) —— `MemOffs_c` 仅被测试台引用（见 4.2），RTL 不使用它；硬件侧真正的内存起点由 IPIC 根据 `NumReg_g => USER_SLV_NUM_REG` 自动算出。因此一旦 `USER_SLV_NUM_REG` 从 8 变 16，硬件内存区会自动移到 `0x40`，但 `MemOffs_c` 仍写死 8、C 驱动 `MM_READER_REGMAP_OFFS` 仍写死 `0x20`，二者都会**与硬件脱节**——这正是「人工同步」风险的具体落点。

**(d) wrapper 如何把位字段接到核心——改寄存器时的参照模板**

新增「可写」寄存器时，要在 wrapper 里从 `reg_wdata` 切出位字段接到核心；新增「只读」寄存器时，则反向把核心信号驱动到 `reg_rdata`。两种方向都已经有现成模板可抄：

[`hdl/axi_mm_reader_wrp.vhd:326-328`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L326-L328) —— 「可写」方向的模板：从 `reg_wdata(RegIdx_X_c)` 切位接到核心端口。

```vhdl
RegCount   => reg_wdata(RegIdx_RegCnt_c)(log2ceil(MaxRegCount_g)-1 downto 0),
Enable     => reg_wdata(RegIdx_Ctrl_c)(BitIdx_Ctrl_Ena_c),
RegCfg_Idx => mem_addr(log2ceil(MaxRegCount_g)+1 downto 2),
```

[`hdl/axi_mm_reader_wrp.vhd:175`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L175) 与 [`hdl/axi_mm_reader_wrp.vhd:183`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L183) —— 「只读」方向的模板：把核心信号 `AxiS_Level` 驱动到 `reg_rdata(RegIdx_Level_c)`。注意它在 `g_axis` 和 `g_naxis` 两个 generate 块里**各写了一遍**，因为 `Level` 寄存器两种输出模式都存在。新加的只读寄存器若也是两种模式共享，应同样在两处都驱动（或放到 generate 块外、与模式无关地驱动）。

```vhdl
reg_rdata(RegIdx_Level_c) <= AxiS_Level;   -- 出现在 g_axis 与 g_naxis 两块中
```

**(e) 核心内部 `DoneCnt`——本讲扩展样例的目标信号**

[`hdl/axi_mm_reader.vhd:85`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L85) —— `DoneCnt` 当前只是核心 record 的一个内部字段，记录本周期已收到的数据字数，外部完全看不到：

```vhdl
DoneCnt : integer range 0 to MaxRegCount_g;
```

它的生命周期由两段代码决定，这两段决定了「软件何时读到什么值」：

[`hdl/axi_mm_reader.vhd:161-166`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L161-L166) —— 每次开始新周期时 `DoneCnt` 清零；每收到一拍有效数据则加 1。

```vhdl
if r.Fsm = Idle_s and r.Start = '1' then
    v.DoneCnt := 0;                       -- 新周期开始：清零
elsif (AxiM_RdDat_Vld = '1') and (Fifo_Rdy = '1') then
    v.DoneCnt := r.DoneCnt + 1;           -- 收到一拍：加 1
end if;
```

由此可推出软件观测值：周期进行中，`DoneCnt` 从 0 单调增长到 `RegCount`；周期结束回到 `Idle_s` 后，若没有新的 `Start`，`DoneCnt` 会**保持**在上次的 `RegCount` 值（只有下一次启动才清零）。这一行为决定了我们在 4.2 里如何为它写一个确定性的断言。

#### 4.1.4 代码实践

> **实践类型**：源码阅读 + 设计型实践（不要求本地综合，但鼓励读者在本地仿真验证）。
>
> **实践目标**：把内部 `DoneCnt` 暴露为只读状态寄存器 `DoneCnt`@索引 5（字节 `0x14`），走完「定义 → 核心 → wrapper」三处 RTL 改动，并验证它落在「零地址位移」的低风险区。

**操作步骤**（以下均为**示例代码**，非仓库原有内容，请勿直接当作已合并的代码）：

1. 在 `hdl/definitions_pkg.vhd` 的 `RegIdx_Level_c` 之后新增索引，并让 `RegCount_c` 跟着派生（示例代码）：

   ```vhdl
   constant RegIdx_Level_c   : natural := 4;
   constant RegIdx_DoneCnt_c : natural := 5;                       -- 新增
   constant RegCount_c       : natural := RegIdx_DoneCnt_c+1;      -- 改为派生自最大索引，= 6
   constant MemOffs_c        : natural := 8;                       -- 不变（仍 ≤ 8，零位移）
   ```

   改完后自检：`RegCount_c = 6`，`log2ceil(6) = 3`，`USER_SLV_NUM_REG = 8` 不变 → **零地址位移**，RegTable 仍在 `0x20`。

2. 在 `hdl/axi_mm_reader.vhd` 实体端口（`AxiS_Level` 之后）加一个输出端口，并在并发赋值区把它接出（示例代码）：

   ```vhdl
   -- 实体端口
   DoneCnt : out std_logic_vector(31 downto 0)
   -- 并发赋值（放在 DoneIrq <= r.DoneIrq; 附近）
   DoneCnt <= std_logic_vector(to_unsigned(r.DoneCnt, DoneCnt'length));
   ```

   `to_unsigned` 来自核心已经 `use` 的 `numeric_std`（见 [`hdl/axi_mm_reader.vhd:12`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L12)），无需额外引库。

3. 在 `hdl/axi_mm_reader_wrp.vhd` 加一个内部信号、在核心实例端口映射里接上、并把值驱动到 `reg_rdata` 的第 5 槽（示例代码）：

   ```vhdl
   signal DoneCnt_int : std_logic_vector(31 downto 0);
   -- ...
   -- 核心实例端口映射新增：
   DoneCnt => DoneCnt_int,
   -- 只读寄存器，两种输出模式共享，放在 generate 块外：
   reg_rdata(RegIdx_DoneCnt_c) <= DoneCnt_int;
   ```

**需要观察的现象**：

- 综合后 `s00_axi` 寄存器区仍为 8 个字（`USER_SLV_NUM_REG` 不变），RegTable 仍从 `0x20` 开始。
- 软件读字节 `0x14` 能拿到当前 `DoneCnt` 值。
- 读 `0x14` 是普通只读（R 模式），**没有副作用**，不会像读 `RdData` 那样弹 FIFO。

**预期结果**：本地仿真（见 4.2 的用例）应能在一次 14 寄存器的读周期结束后，从 `0x14` 读回 14。

> 若无法本地综合/仿真，明确标注「待本地验证」——不要假装已运行。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `RegIdx_DoneCnt_c` 设成 7（而非 5），`USER_SLV_NUM_REG` 会变吗？RegTable 地址会位移吗？

**参考答案**：不会。`RegCount_c = 8`，`log2ceil(8) = 3`，`USER_SLV_NUM_REG = 2**3 = 8`，仍零位移。索引 5、6、7 三个槽位都是「免费」的。只有当 `RegCount_c` 达到 9 时，`log2ceil(9) = 4`，`USER_SLV_NUM_REG` 才跳到 16。

**练习 2**：为什么新增第 4 个寄存器（使 `RegCount_c = 9`）后，`MemOffs_c` 和 C 宏 `MM_READER_REGMAP_OFFS` 都会失效？

**参考答案**：`USER_SLV_NUM_REG` 从 8 跳到 16，硬件内存区起点由 IPIC 自动移到 `16 × 4 = 0x40`；但 `MemOffs_c` 硬编码为 8、`MM_READER_REGMAP_OFFS` 硬编码为 `0x20`，二者都不会自动跟随，必须人工改成 16 / `0x40`，否则测试台写错 RegTable 地址、驱动写错寄存器表偏移。

**练习 3**：新增的只读寄存器赋值为什么放在 generate 块外、而不是像 `Level` 那样在 `g_axis`/`g_naxis` 里各写一遍？

**参考答案**：因为该寄存器与输出模式无关（两种模式都应能读），放在架构体层（generate 块外）一次赋值即可，比在两个互斥 generate 块里重复写更不易漏改。`Level` 之所以在两块里各写一遍，只是作者当时的写法选择，本质上也可以提到块外。

### 4.2 测试与仿真维护

#### 4.2.1 概念说明

RTL 改完不算完，必须有用例证明新行为正确。本 IP 的测试台 `tb/top_tb.vhd` 是**自校验**的：它在 DUT 的两个 AXI 接口上各挂替身（BFM），用两个并发进程配合，把一次回归切成若干互不串扰的用例。理解它的握手机制，是「安全加用例」的前提。

关键认知有两条：

1. **断言不符即失败**：测试台用 `psi_tb` 的比较函数（如 `axi_single_expect`、`StdlvCompareInt`）做断言；不符即向 transcript 打 `###ERROR###`，被 `run.tcl` 的 `run_check_errors "###ERROR###"` 捕获而令 CI 失败（见 [u1-l3](u1-l3-running-simulation.md)）。所以「加一个断言」就是「加一个自校验点」。
2. **`config.tcl` 只管文件，不管用例**：`sim/config.tcl` 登记的是「编译哪些 `.vhd`、跑哪些 generic 组合」，与测试台内部有多少用例**无关**。只要你不新增源文件、不改 generic，`config.tcl` **无需任何修改**——这是本模块最重要的结论。

#### 4.2.2 核心流程

为新增行为编写验证的流程：

1. **选位置**：若新行为能挂到现有用例（如「单次读」用例）上，就在该用例的 `CheckResults` 之后加一条断言；若新行为是独立场景，则新增一个 `StimCase` 编号并在 `p_control`/`p_spi` 两端同步处理。
2. **加断言**：在 `p_control` 里用 `axi_single_expect(RegIdx_X_c*4, 期望值, ...)` 或 `axi_single_read` 轮询。
3. **配仿真**：确认 `config.tcl`——只要没新增 `.vhd` 文件，**不改**；AXIS/AXIMM 双跑自动覆盖两种模式。
4. **跑回归**：本地 `source sim/run.tcl`（Modelsim）或 `runGhdl.tcl`（GHDL），检查 transcript 无 `###ERROR###`。

#### 4.2.3 源码精读

**(a) 握手信号——用例之间不串扰的基石**

[`tb/top_tb.vhd:77-78`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L77-L78) —— 两个整数信号 `StimCase`/`RespCase` 初值 `-1`，充当 `p_control`（发激励）与 `p_spi`（回送数据）之间的阻塞握手：

```vhdl
signal StimCase : integer := -1;
signal RespCase : integer := -1;
```

`p_control` 在进入一个用例前置 `StimCase <= N`，`p_spi` 在完成该用例的数据回送后置 `RespCase <= N`；`p_control` 用 `wait until rising_edge(aclk) and RespCase = N` 等待对端完成，确保两进程同步、用例边界清晰。

**(b) 配置阶段——新寄存器需要在此预热**

[`tb/top_tb.vhd:264-269`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L264-L269) —— `p_control` 的配置阶段：写 `RegCnt=14`、循环写 14 项 RegTable、置使能。注意 RegTable 写地址用的是 `(MemOffs_c+i)*4`——这正是 4.1 提到的「`MemOffs_c` 仅测试台使用」的落点：

```vhdl
axi_single_write(RegIdx_RegCnt_c*4, 14, axi_ms, axi_sm, aclk);
for i in 0 to 13 loop
    axi_single_write((MemOffs_c+i)*4, 16#00AB0000#+16*i, axi_ms, axi_sm, aclk);
end loop;
axi_single_write(RegIdx_Ctrl_c*4, 1, axi_ms, axi_sm, aclk);
```

**(c) 用例边界——在哪里插入新断言**

[`tb/top_tb.vhd:271-277`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L271-L277) —— 「单次读」用例：置 `StimCase<=1`、触发 `Trig`、调 `CheckResults` 校验 14 个值、然后 `wait until ... RespCase = 1` 等待 `p_spi` 完成。新断言最适合插在 `CheckResults` 之后、`wait` 之前：

```vhdl
StimCase <= 1;
ClockedWaitTime(100 ns, aclk);
PulseSig(Trig, aclk);
CheckResults(0, 1, ...);                 -- 校验 14 个读回值
wait until rising_edge(aclk) and RespCase = 1;
```

**(d) AXIMM 路径的校验过程——RV 读序的活教材**

[`tb/top_tb.vhd:113-133`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L113-L133) —— `CheckResultsAxiMM` 先轮询 `Level>0`，再**先 `axi_single_expect` 读 `RdLast`（peek），后读 `RdData`（pop）**。新寄存器若涉及读副作用，必须照此读序；若无副作用（如 `DoneCnt`），可直接用 `axi_single_expect` 一次读完：

```vhdl
axi_single_expect(RegIdx_RdLast_c*4, choose(i=13,1,0), ...);  -- 先 peek Last
axi_single_expect(RegIdx_RdData_c*4, start+i*step, ...);      -- 再 pop Data
```

**(e) 校验分派——两种输出模式共享同一期望**

[`tb/top_tb.vhd:135-150`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L135-L150) —— `CheckResults` 按 generic `OutputType_g` 分派到 AXIS 或 AXIMM 两条路径，对同一期望值 `start+i*step` 与末拍 `Last` 做相同断言。新增的状态寄存器（如 `DoneCnt`）若与输出模式无关，其断言可放在 `CheckResults` 之外、用例体内直接 `axi_single_expect`，两种模式都会跑到（因为 `config.tcl` 会跑两遍）。

**(f) `config.tcl` 的边界——什么时候才需要改它**

[`sim/config.tcl:40-50`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L40-L50) —— 这里登记的是**源文件**（`-tag src` 列出 `definitions_pkg.vhd`/`axi_mm_reader.vhd`/`axi_mm_reader_wrp.vhd`，`-tag tb` 列出 `top_tb.vhd`）。只要你不新增 `.vhd` 文件，这三行无需改动。

[`sim/config.tcl:52-56`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L52-L56) —— 这里登记的是**测试台跑哪几组 generic**（`OutputType_g=AXIS` 与 `AXIMM`）。新增用例或新增寄存器都**不需要**改这里；只有当你新增了一个独立测试台实体（比如 `top_tb_status.vhd`）时，才需要 `add_sources` 登记 + `create_tb_run`。

```tcl
psi::sim::create_tb_run "top_tb"
tb_run_add_arguments "-gOutputType_g=AXIS" "-gOutputType_g=AXIMM"
```

#### 4.2.4 代码实践

> **实践目标**：为 4.1 暴露的 `DoneCnt` 寄存器在「单次读」用例里加一个确定性断言，并确认 `config.tcl` 无需改动。

**操作步骤**（示例代码，非仓库原有内容）：

1. 在 [`tb/top_tb.vhd:276`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L276) 的 `CheckResults(...)` 之后、`wait until ... RespCase = 1` 之前，插入一条断言（示例代码）：

   ```vhdl
   CheckResults(0, 1, m_axis_tvalid, m_axis_tready, m_axis_tlast, m_axis_tdata, axi_ms, axi_sm, aclk);
   -- 周期已结束、回到 Idle，DoneCnt 应保持为本周期读取的字数 14
   axi_single_expect(RegIdx_DoneCnt_c*4, 14, axi_ms, axi_sm, aclk, "DoneCnt after cycle");
   wait until rising_edge(aclk) and RespCase = 1;
   ```

   选这个位置的理由：`CheckResults` 已确认 14 个数据全部出 FIFO，此时周期必然已结束（`WaitDone_s` → `Idle_s`），`DoneCnt` 保持为 14 且尚未被下一次启动清零——一个完全确定的可观测点。

2. 检查 `sim/config.tcl`：本次扩展**没有新增任何 `.vhd` 文件**（`DoneCnt` 只是把核心已有内部信号引出），故 `config.tcl` **无需任何改动**，AXIS/AXIMM 双跑会自动覆盖两种模式下的新断言。

**需要观察的现象**：

- 仿真 transcript 中 `DoneCnt after cycle` 这条断言不报 `###ERROR###`。
- 若误把断言放在 `CheckResults` **之前**（周期未结束、数据未收齐），`DoneCnt` 可能小于 14，断言会失败——这正是「断言位置要与信号生命周期匹配」的体现。

**预期结果**：两种输出模式下，`DoneCnt` 均读回 14。若本地无法运行仿真，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么新断言放在 `CheckResults` 之后、而不是 `PulseSig(Trig)` 之后立刻？

**参考答案**：`Trig` 之后数据尚未收齐，`DoneCnt` 还在从 0 增长，值不确定；必须等到周期结束（`CheckResults` 已消费完全部数据、状态机回到 `Idle_s`），`DoneCnt` 才稳定在 `RegCount`（=14），此时断言才是确定性的。

**练习 2**：如果这次扩展新增了一个独立的测试台文件 `top_tb_status.vhd`，`config.tcl` 需要改哪几处？

**参考答案**：需要在 `-tag tb` 的 `add_sources` 里登记 `top_tb_status.vhd`，并用 `create_tb_run "top_tb_status"`（及相应 `tb_run_add_arguments`）声明它的运行配置。仅加用例或加寄存器则不需要任何此类改动。

**练习 3**：`p_spi` 进程需要为 `DoneCnt` 加配套代码吗？

**参考答案**：不需要。`p_spi` 只扮演 `m00_axi` 从机、回送读数据；`DoneCnt` 是核心内部计数，由 `p_control` 经 `s00_axi` 读取并校验，与 `p_spi` 无关。这正体现了「新断言挂在现有用例上」时只需动 `p_control` 一侧的便利。

### 4.3 驱动与文档同步

#### 4.3.1 概念说明

RTL 与测试都改完后，最后一环是让**软件侧**能用到新寄存器，并让**文档**反映新的寄存器地图。这一环看似琐碎，却是「能不能交付」的关键：驱动地址宏与 RTL 字索引错一位，软件就会读到完全错误的寄存器；文档少一行，下游使用者就无从知道新寄存器的存在。

C 驱动 `drivers/axi_mm_reader` 是一层**极薄的封装**：每个 API 即「按地址常量算字节地址 + 一次 `Xil_In32`/`Xil_Out32`」。地址常量与 RTL `definitions_pkg.vhd` 的字索引严格对应（字节地址 = 字索引 × 4）。因此新增寄存器在驱动侧的工作量极小——加一个 `#define`、加一个 getter——但必须**与 RTL 同步**。

文档 `doc/Documentation.md` 的寄存器表是软件使用者的「编程契约」，新增寄存器必须补一行，并写清楚模式（R/W/RW/RV）与位域。

#### 4.3.2 核心流程

驱动与文档同步的流程：

1. **加地址宏**：在 `axi_mm_reader.h` 加 `#define MM_READER_X_REG <字节地址>`（= 字索引 × 4）。
2. **加函数声明**：在 `.h` 声明 getter（或 setter）原型与注释。
3. **加函数实现**：在 `.c` 套用 `MmReader_GetLevel` 的模板实现。
4. **补文档**：在 `Documentation.md` 寄存器表加一行（地址、名字、模式、位域、说明）。
5. **自检一致性**：RTL 字索引 × 4 = C 宏值 = 文档地址，三者必须完全相等。

#### 4.3.3 源码精读

**(a) 地址宏——驱动与 RTL 的对齐表**

[`drivers/axi_mm_reader/src/axi_mm_reader.h:31-37`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.h#L31-L37) —— 寄存器字节地址宏，与 `definitions_pkg.vhd` 的字索引一一对应（`RegIdx_Level_c=4` → `0x10`，`RegIdx_*` 字索引 × 4 即得此处字节地址）：

```c
#define MM_READER_ENA_REG      0x00
#define MM_READER_REG_CNT_REG  0x04
#define MM_READER_RD_DATA_REG  0x08
#define MM_READER_RD_LAST_REG  0x0C
#define MM_READER_LEVEL_REG    0x10
#define MM_READER_REGMAP_OFFS  0x20
```

新增 `DoneCnt`@索引 5 → 字节 `5 × 4 = 0x14`，应在此加 `#define MM_READER_DONECNT_REG 0x14`。

**(b) 错误码——契约违反的描述**

[`drivers/axi_mm_reader/src/axi_mm_reader.h:23-29`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.h#L23-L29) —— 错误码枚举。若新寄存器有前置条件（如「必须禁用 IP 才能写」），应在此追加相应错误码；纯只读状态寄存器（如 `DoneCnt`）无前置条件，无需新增。

**(c) 只读 getter 的模板——照抄即可**

[`drivers/axi_mm_reader/src/axi_mm_reader.c:62-67`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L62-L67) —— `MmReader_GetLevel` 是最简 getter 的标准模板：一次 `Xil_In32`、回填出参、返回 `Success`。新只读寄存器的 getter 几乎照抄：

```c
MmReader_ErrCode MmReader_GetLevel(const uint32_t baseAddr, uint32_t* const level_p) {
    *level_p = Xil_In32(baseAddr + MM_READER_LEVEL_REG);
    return MmReader_Success;
}
```

**(d) 读副作用读序——别在新寄存器上重蹈覆辙**

[`drivers/axi_mm_reader/src/axi_mm_reader.c:84-87`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L84-L87) —— `ReadFifoEntry` 严格「先读 `RdLast`、再读 `RdData`」，因为读 `RdData` 会弹 FIFO。新增寄存器时务必判断它是否带副作用：若无（如 `DoneCnt`），可任意顺序、可轮询；若有，则必须像这里一样在驱动里强制读序、并在文档里标注 RV 模式。

```c
//Read last first (because reading data removes the FIFO entry)
reg = Xil_In32(baseAddr + MM_READER_RD_LAST_REG);
*last_p = (bool)reg;
*data_p = Xil_In32(baseAddr + MM_READER_RD_DATA_REG);
```

**(e) 文档寄存器表——编程契约**

[`doc/Documentation.md:70-79`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L70-L79) —— 寄存器表。新增寄存器须在此加一行，写清楚地址、名字、模式（R/W/RW/RV）、位域、说明。模式标记尤其重要——RV 会提醒软件「读有副作用」。

[`doc/Documentation.md:82`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L82) —— 文档已明确写出 AXIMM 下 `RdLast` 必须先于 `RdData` 读取。新寄存器若有类似约束，必须在此同等醒目地说明。

#### 4.3.4 代码实践

> **实践目标**：为 `DoneCnt` 寄存器补齐 C 驱动（宏 + getter）与文档一行，并自检三者地址一致。

**操作步骤**（示例代码，非仓库原有内容）：

1. 在 `drivers/axi_mm_reader/src/axi_mm_reader.h` 加宏与声明（示例代码）：

   ```c
   #define MM_READER_DONECNT_REG 0x14
   /* ... */
   MmReader_ErrCode MmReader_GetDoneCnt(const uint32_t baseAddr, uint32_t* const doneCnt_p);
   ```

2. 在 `drivers/axi_mm_reader/src/axi_mm_reader.c` 套用 `GetLevel` 模板实现（示例代码）：

   ```c
   MmReader_ErrCode MmReader_GetDoneCnt(const uint32_t baseAddr, uint32_t* const doneCnt_p) {
       *doneCnt_p = Xil_In32(baseAddr + MM_READER_DONECNT_REG);
       return MmReader_Success;
   }
   ```

3. 在 `doc/Documentation.md` 寄存器表 `Level` 行之后、`Addr[0]` 行之前加一行（示例代码）：

   ```markdown
   | 0x14 | DoneCnt | R | 31:0 | Number of 32-bit words received in the current/last read cycle (held until next cycle starts) |
   ```

**需要观察的现象**：

- 自检「RTL 字索引 × 4 = C 宏 = 文档地址」：`5 × 4 = 0x14 = 0x14`，三者一致。
- `DoneCnt` 为 R 模式（只读、无副作用），文档不标 RV，驱动 getter 无需检查使能状态或读序。

**预期结果**：Vitis BSP 重新生成后，应用代码调用 `MmReader_GetDoneCnt(baseAddr, &n)` 即可读到当前/上一周期的接收字数。若本地无 Vitis 环境，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：若新增的是一个「必须禁用 IP 才能写」的配置寄存器，驱动 setter 应参照哪个现有函数？

**参考答案**：参照 `MmReader_SetRegTable`（见 [`drivers/axi_mm_reader/src/axi_mm_reader.c:37-60`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L37-L60)）——先 `MmReader_GetEnable` 查状态，若已使能则返回 `MmReader_IpMustBeDisabled`，否则才执行写入。因为核心仅在 `Idle_s` 采样配置，运行中改配置不安全。

**练习 2**：为什么 `DoneCnt` 的 getter 不需要像 `ReadFifoEntry` 那样先查 `Level`、再按特定读序？

**参考答案**：`DoneCnt` 是普通只读寄存器（R 模式），读取无副作用、与 FIFO 状态无关，可任意时刻直接 `Xil_In32`。`ReadFifoEntry` 之所以复杂，是因为 `RdData` 是 RV（读即弹），必须先 peek `RdLast` 再 pop `RdData`，且要先确认 FIFO 非空。

**练习 3**：文档里把 `DoneCnt` 误标成 RV 模式会有什么后果？

**参考答案**：软件使用者会误以为读 `DoneCnt` 有副作用（如清零或弹栈），从而写出不必要的「先读别的再读它」或避免重复读的防御性代码，造成无谓复杂度；更糟的是可能误以为读它会清零计数而放弃用它做轮询。模式标记必须与 RTL 实际行为严格一致。

## 5. 综合实践

> **贯穿任务**：设计一个「读取内部状态」的小扩展，端到端走完「定义 → 核心 → wrapper → 测试 → 驱动 → 文档」全链路，并判断每一步的风险等级。

**任务背景**：4.1 已示范了暴露 `DoneCnt`（索引 5）。现在请你设计**第二个**只读状态寄存器，把核心 FSM 的当前状态也暴露给软件，方便诊断「IP 现在卡在哪个状态」。

**要求**：

1. **定索引与判风险**：把新寄存器放在索引 6（字节 `0x18`）。计算此时 `RegCount_c`、`USER_SLV_NUM_REG`，判断是否仍属「零地址位移」低风险区。
2. **核心改动**：`axi_mm_reader.vhd` 的 FSM 类型 `Fsm_t` 定义在 [`hdl/axi_mm_reader.vhd:74`](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L74)（`Idle_s/ReadAddr_s/SetCmd_s/ApplyCmd_s/WaitDone_s`）。规划如何把当前状态 `r.Fsm` 编码成一个 32 位只读值输出（提示：可用 `to_unsigned(Fsm_t'pos(r.Fsm), 32)` 把枚举转成整数）。
3. **wrapper 改动**：参照 4.1 的接线方式，加内部信号、端口映射、`reg_rdata(RegIdx_X_c)` 赋值。
4. **测试用例**：在 `top_tb.vhd` 的「单次读」用例中，规划如何在不同时机读取该寄存器并断言它处于 `Idle_s`（周期结束后）——给出断言插入位置与期望值。
5. **config.tcl**：确认本次扩展是否需要改 `sim/config.tcl`，并说明理由。
6. **驱动与文档**：写出新增的 C 宏、getter 原型与文档表格行。
7. **风险复盘**：如果之后还要加第 3 个状态寄存器（索引 7）、以及第 4 个（使 `RegCount_c=9`），分别属于哪个风险等级、需要额外同步哪些常量？

**交付物**：一份「文件清单 + 每个文件的关键改动点」的 markdown 表格，外加一段「风险等级与级联影响」的说明。完成后，你应当能清晰地说出：在这个 IP 里，**索引 5/6/7 是「免费」的，索引 8 以后每一次新增都要重新审视整张内存映射**。

> 本任务为设计型实践，不要求本地综合；若你手头有 Vivado/PsiSim 环境，鼓励把改动落到代码并跑一次 `source sim/run.tcl` 验证 transcript 无 `###ERROR###`。否则明确标注「待本地验证」。

## 6. 本讲小结

- 新增寄存器是一条**端到端链路**：`definitions_pkg → 核心 → wrapper → top_tb → 驱动 → 文档`，`definitions_pkg.vhd` 的 `RegIdx_*` 常量是唯一事实来源，漏掉链路上任何一处都会导致软硬件不一致。
- **风险判据**：`USER_SLV_NUM_REG = 2**log2ceil(RegCount_c)` 向上取整到 2 的幂，当前 = 8；新增寄存器填到索引 5/6/7 是「零地址位移」低风险改动，一旦 `RegCount_c` 达到 9，`USER_SLV_NUM_REG` 跳到 16，RegTable 内存区整体后移。
- `MemOffs_c`（=8）与 C 宏 `MM_READER_REGMAP_OFFS`（=`0x20`）是**硬编码**常量，不随 `USER_SLV_NUM_REG` 自动变化——跨过 2 的幂边界时必须人工同步这两处（以及文档地址）。
- 只读状态寄存器（如 `DoneCnt`）只需在核心加输出端口、在 wrapper 驱动 `reg_rdata` 槽位，套 `Level` 的模板即可；驱动 getter 套 `MmReader_GetLevel` 模板。
- 测试侧：在 `top_tb.vhd` 用 `axi_single_expect` 加断言，断言位置须与信号生命周期匹配；只要不新增 `.vhd` 文件，`sim/config.tcl` **无需改动**，AXIS/AXIMM 双跑自动覆盖。
- 有读副作用的寄存器必须像 `RdData`/`RdLast` 那样在驱动里强制读序、在文档里标注 RV；普通只读寄存器（如 `DoneCnt`）则无此约束。

## 7. 下一步学习建议

- **回头精读依赖库**：本讲的扩展大量依赖 `psi_common`（`psi_common_axi_slave_ipif`、`psi_common_axi_master_simple`、`psi_common_sync_fifo`、`psi_common_tdp_ram`）。若你想做更深度的二次开发（例如改 FIFO 深度行为、改 AXI 突发长度），建议直接阅读 `psi_common` 源码，重点看 `axi_slave_ipif` 如何解码寄存器区与内存区、`sync_fifo` 的 `OutLevel` 如何回灌背压。
- **做一次真实的「跨边界」扩展练习**：尝试新增第 4 个寄存器（使 `RegCount_c=9`），亲手同步 `MemOffs_c`、`MM_READER_REGMAP_OFFS`、文档地址、`top_tb` 的 `(MemOffs_c+i)*4`，体会「级联影响」的真实工作量。
- **打包并上板**：把扩展后的 IP 用 `scripts/package.tcl`（见 [u1-l4](u1-l4-ip-packaging.md)、[u3-l3](u3-l3-parameters-gui.md)）重新打包，在 Block Design 里实例化（见 [u3-l4](u3-l4-ipxact-block-design.md)），导出 XSA 后在 Vitis 用新驱动 API 验证端到端链路闭合。
- **回到全册复盘**：至此你已走完「认识项目 → 拆解核心模块 → 驱动/测试/打包 → 二次开发」的完整闭环。建议重读 [u2-l1](u2-l1-architecture-dataflow.md) 的架构图，确认自己能在脑中把每一个寄存器、每一条数据通路、每一处扩展落点都对应到具体源码行。

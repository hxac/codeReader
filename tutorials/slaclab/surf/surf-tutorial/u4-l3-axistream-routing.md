# AXI-Stream 路由：Mux / DeMux / Gearbox

## 1. 本讲目标

本讲承接 [u4-l1](u4-l1-axistream-records.md)（AXI-Stream 记录与 `AxiStreamConfigType`）和 [u4-l2](u4-l2-axistream-fifo-pipeline.md)（FIFO / Pipeline / Resize），把「一条流」推进到「多条流之间的路由与整形」。

学完本讲，你应当能够：

- 说清 `AxiStreamMux` 如何把多条入流仲裁合并成一条出流，以及它默认的「按帧原子切换」为何能保证帧序完整。
- 说清 `AxiStreamDeMux` 如何依据 `tDest` 把一条入流分发到多条出流，并理解 INDEXED / ROUTED / DYNAMIC 三种解码模式。
- 说清 `AxiStreamGearbox` 在「字宽非整数倍」时如何用移位寄存器做位宽变换，以及它何时会把工作让给更省 LUT 的 `AxiStreamResize`。
- 理解 `AxiStreamGearboxPack` 这种「抽取子字段再打包」的特殊位宽变换。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，AXI-Stream 是「单向握手数据流」。** 每条流由 `AxiStreamMasterType`（生产方驱动 `tValid` + 数据与侧带）和 `AxiStreamSlaveType`（消费方驱动 `tReady`）组成。一拍数据只在 `tValid='1'` 且 `tReady='1'` 同时成立时才算「成交」。本讲的 Mux / DeMux / Gearbox 都是在这个握手规则之上做搬运，不破坏握手语义。

**第二，`tDest` 是「路由标签」而非数据。** 在 SURF 的 AXI-Stream 里，`tDest` 最多 8 位，常被当作虚拟通道号或目标端口号。一条物理流可以通过 `tDest` 在逻辑上承载多个目的地。Mux/DeMux 正是围绕 `tDest` 做收与发的。

**第三，位宽变换分两种几何关系。** 若两侧字节宽互为整数倍（如 4↔8、4↔16），只是把字节简单拼接或拆开；若不是整数倍（如要把一段 12 位的子字段塞进 16 位总线），就需要一个「移位缓冲」来逐拍对齐。前者由 `AxiStreamResize` 高效处理，后者由 `AxiStreamGearbox` 处理。

本讲还用到两个前置概念，详见 [u4-l1](u4-l1-axistream-records.md)：

- `AxiStreamConfigType`：编译期描述一条流的 7 个字段（`TDATA_BYTES_C`、`TDEST_BITS_C`、`TKEEP_MODE_C` 等）。
- `tKeep` 的四种模式（`TKEEP_NORMAL_C` / `COMP_C` / `FIXED_C` / `COUNT_C`）以及 `genTKeep` / `getTKeep` 这对互逆函数。

## 3. 本讲源码地图

本讲涉及 4 个 RTL 文件，都在 `axi/axi-stream/rtl/` 下：

| 文件 | 角色 |
|------|------|
| `AxiStreamMux.vhd` | 多入单出：把 `NUM_SLAVES_G` 条入流仲裁合并成 1 条出流，可改写 `tDest`/`tId`。 |
| `AxiStreamDeMux.vhd` | 单入多出：按 `tDest` 把 1 条入流分发到 `NUM_MASTERS_G` 条出流。 |
| `AxiStreamGearbox.vhd` | 通用位宽变换：整数倍时委托给 `AxiStreamResize`，非整数倍时用移位寄存器做位级打包/拆包。 |
| `AxiStreamGearboxPack.vhd` | 特殊打包：抽取 `tData` 中 `[RANGE_HIGH_G:RANGE_LOW_G]` 这段子字段，重新拼装进满宽流。 |

它们都遵循 [u1-l5](u1-l5-two-process-style.md) 的双进程骨架（`RegType` + `REG_INIT_C` + `r/rin` + `comb`/`seq`），并且末尾都例化一个可选的 `AxiStreamPipeline` 来放松时序。理解 Mux/DeMux 时还会用到 `base/general/rtl/ArbiterPkg.vhd` 里的 `arbitrate` 过程（轮询仲裁，[u2-l5](u2-l5-general-primitives.md) 讲过仲裁器）。

## 4. 核心概念与源码讲解

### 4.1 流复用仲裁：AxiStreamMux

#### 4.1.1 概念说明

`AxiStreamMux` 解决的问题是：多个数据源（例如多个虚拟通道、多块板卡、多个 DMA 描述符）都要往同一条物理流上送数据，而下游只有一条入口。需要一个「调度员」在多个请求者之间做选择，把选中者的数据接到输出，并告诉其他请求者「请等一下」（把它们的 `tReady` 拉低）。

关键设计取舍有两个：

- **仲裁粒度**：Mux 默认是「按帧」仲裁的——一旦选中某个源，就会把它的整帧（直到 `tLast`）送完，中途不切换。这避免了把两帧的数据交错拼到一起（除非你显式打开 `ILEAVE_EN_G` interleave 模式）。这正是后面「DeMux→Mux 回环验证帧序」能成立的前提。
- **路由标签改写**：合并后，下游需要知道这一拍/这一帧来自哪个源。Mux 提供三种给输出 `tDest` 赋值的方式：`INDEXED`（把选中的源编号写进 `tDest` 的某几位）、`ROUTED`（按一张固定表改写每一位）、`PASSTHROUGH`（原样透传源端 `tDest`）。`tId` 同理。

#### 4.1.2 核心流程

Mux 的核心是一个「锁定式轮询仲裁器」，伪代码如下：

```
每拍 (comb 进程):
  默认把所有源的 tReady 清 0          # 本拍还没决定服务谁
  若下游 tReady=1，则把输出 tValid 清 0 # 输出腾出位置

  收集 requests[i] = 源i.tValid 且 未被 disableSel 屏蔽

  if 当前未锁定 (valid=0):
      用 arbitrate(requests, 上次选中者) 选出本轮胜者 ackNum
      若有请求则 valid=1，锁定到该源
  else (valid=1, 已锁定在某源):
      若已开 ILEAVE 且满足重仲裁条件 -> valid=0 (下拍重新选)
      若下游有空 且 胜者有数据 -> 接收该源数据送到输出
      若该拍是 tLast -> 锁定释放 (valid=0)，下拍重新仲裁
```

「锁定 + 遇 `tLast` 释放」就是帧原子性的来源。`disableSel` 是一个屏蔽位向量，运行时可临时禁止某个源参与仲裁；`PRIORITY_G` 则在静态层面给不同源赋优先级。

#### 4.1.3 源码精读

先看实体与泛型。Mux 最核心的几个泛型是 `NUM_SLAVES_G`、`MODE_G`（tDest 赋值方式）和 `ILEAVE_EN_G`（是否允许帧内交错）：

实体声明与泛型见 [axi/axi-stream/rtl/AxiStreamMux.vhd:27-81](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMux.vhd#L27-L81)。其中 `MODE_G` 的三种取值在注释里讲得很清楚：

```vhdl
-- In INDEXED mode, the output TDEST is set based on the selected slave index (default)
-- In ROUTED mode, TDEST is set according to the TDEST_ROUTES_G table
-- In PASSTHROUGH mode, TDEST is passed through from the slave untouched
MODE_G         : string := "INDEXED";
```

两个编译期常量决定了仲裁向量的位宽——`DEST_SIZE_C` 是「表示源个数所需的最少位数」，`ARB_BITS_C` 是把它向上取整到 2 的幂（因为 `arbitrate` 内部用 `rotate_right` 做轮询，要求位宽是 2 的幂）：

```vhdl
constant DEST_SIZE_C : integer := bitSize(NUM_SLAVES_G-1);
constant ARB_BITS_C  : integer := 2**DEST_SIZE_C;
```

见 [axi/axi-stream/rtl/AxiStreamMux.vhd:85-86](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMux.vhd#L85-L86)。

状态记录里，`ackNum` 记住「当前锁定在哪个源」，`valid` 是锁定标志，`arbCnt` 用于 interleave 模式下的「每 N 拍重仲裁」计数：

```vhdl
type RegType is record
   acks   : slv(ARB_BITS_C-1 downto 0);
   ackNum : slv(DEST_SIZE_C-1 downto 0);
   valid  : sl;
   arbCnt : slv(11 downto 0);
   slaves : AxiStreamSlaveArray(NUM_SLAVES_G-1 downto 0);
   master : AxiStreamMasterType;
end record RegType;
```

见 [axi/axi-stream/rtl/AxiStreamMux.vhd:88-103](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMux.vhd#L88-L103)。注意 `REG_INIT_C` 里 `ackNum` 初值是 `NUM_SLAVES_G-1`，这样首次 `arbitrate` 从 `ackNum+1 = 0` 开始（取模后），即第一个被考虑的是 0 号源。

仲裁的真正发生在这段 comb 逻辑——先把各源请求收集进 `requests`，再调用 `arbitrate`：

```vhdl
-- Format requests
requests := (others => '0');
for i in 0 to (NUM_SLAVES_G-1) loop
   requests(i) := sAxisMastersTmp(i).tValid and not intDisableSel(i);
end if;
...
-- Arbitrate between requesters
if ((v.valid = '0' and REARB_DELAY_G = false) or r.valid = '0') then
   v.arbCnt := (others => '0');
   arbitrate(requests, r.ackNum, v.ackNum, v.valid, v.acks);
end if;
```

见 [axi/axi-stream/rtl/AxiStreamMux.vhd:254-303](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMux.vhd#L254-L303)。`arbitrate` 来自 `ArbiterPkg`，其语义是：以「上次选中者 +1」为最高优先级，做轮询（round-robin）选择——这正是「公平性」的来源，任何一个持续请求的源都不会被饿死。详见 [base/general/rtl/ArbiterPkg.vhd:69-85](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/base/general/rtl/ArbiterPkg.vhd#L69-L85)。

数据搬运与帧原子性：选中源后，若下游有空，就把该源数据送到输出、给该源 `tReady=1`，并在遇到 `tLast` 时释放锁定：

```vhdl
v.slaves(conv_integer(r.ackNum)).tReady := '1';
v.master := selData;
v.arbCnt := r.arbCnt + 1;
if selData.tLast = '1' then
   v.valid := '0';   -- 帧结束，下拍重新仲裁
```

见 [axi/axi-stream/rtl/AxiStreamMux.vhd:271-292](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMux.vhd#L271-L292)。

INDEXED 模式如何把源编号写进 `tDest`：把 `tDest` 高位清零，再把 `ackNum` 写到 `[TDEST_LOW_G + DEST_SIZE_C -1 : TDEST_LOW_G]` 这几位：

```vhdl
if MODE_G = "INDEXED" then
   selData.tDest(7 downto TDEST_LOW_G)                         := (others => '0');
   selData.tDest(DEST_SIZE_C+TDEST_LOW_G-1 downto TDEST_LOW_G) := r.ackNum;
end if;
```

见 [axi/axi-stream/rtl/AxiStreamMux.vhd:235-243](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMux.vhd#L235-L243)。这段是后面「Mux 输出 `tDest` 能被 DeMux 还原」的关键：源 i 选中时，`tDest` 的低 `DEST_SIZE_C` 位就等于 i。

最后，静态优先级 `PRIORITY_G` 在一个独立组合进程里算出 `intDisableSel`：只要存在比某源更高优先级的源正在请求，就把该源的请求屏蔽掉（见 [axi/axi-stream/rtl/AxiStreamMux.vhd:183-202](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMux.vhd#L183-L202)）。不设 `PRIORITY_G` 时所有源等优先级，退化为纯轮询。

#### 4.1.4 代码实践

**实践目标**：验证 Mux 的「轮询仲裁顺序」与「INDEXED 模式下输出 `tDest` 等于源编号」。

**操作步骤**（基于仓库已有的 cocotb 测试 `tests/axi/axi_stream/test_AxiStreamMux.py`）：

1. 先按 [u9-l1](u9-l1-cocotb-toolchain.md) 的方法生成 ruckus 源缓存：
   ```bash
   make MODULES=$PWD import
   ```
2. 运行 Mux 的回归测试：
   ```bash
   ./.venv/bin/python -m pytest -q tests/axi/axi_stream/test_AxiStreamMux.py
   ```
3. 打开 [tests/axi/axi_stream/test_AxiStreamMux.py](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamMux.py)，找到 `indexed` 用例：两个源同时发数据，断言输出顺序遵循配置的 `PRIORITY_G` 与 `disableSel`。

**需要观察的现象**：当 `disableSel` 全 0 且两源等优先级时，由于 `ackNum` 初值为 `NUM_SLAVES_G-1`，第一个被服务的是 0 号源，之后按轮询在两源间切换；每帧完整送出后才切换源（不会出现两帧交错）。

**预期结果**：测试全部通过（`PASSED`）。如果你修改测试里某源的 `PRIORITY_G` 为更高值，应观察到该源总是优先被服务。**待本地验证**：具体的 `pytest` 输出与用时取决于本地 GHDL/cocotb 环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ARB_BITS_C` 要取 2 的幂，而不是直接等于 `NUM_SLAVES_G`？

**参考答案**：因为 `arbitrate` 内部调用 `priorityEncode`，后者用 `rotate_right(unsigned(v), p)` 做循环移位来实现「从上次选中者的下一位开始轮询」。循环移位要求位宽是 2 的幂才有正确的取模语义；当 `NUM_SLAVES_G` 不是 2 的幂时（如 3、5），需要把请求向量补齐到最近的 2 的幂，多出来的高位恒为 0（无对应源），不影响结果。

**练习 2**：若想让 Mux 在传输过程中允许「中断当前帧、切换到另一个源」，需要打开哪个泛型？它会带来什么风险？

**参考答案**：打开 `ILEAVE_EN_G=true`，并可配合 `ILEAVE_ON_NOTVALID_G`（源端 `tValid` 掉落时重仲裁）与 `ILEAVE_REARB_G`（每 N 拍强制重仲裁）。风险是输出流会交错来自不同源的数据，下游若按帧解析会出错——因此只有当下游能用 `tDest`/`tId` 区分交错帧时才可使用。这就是 `AxiStreamGearbox.vhd` 注释里「Resizer should not be used when interleaving tDests」的同一类约束。

### 4.2 TDEST 分发：AxiStreamDeMux

#### 4.2.1 概念说明

`AxiStreamDeMux` 是 Mux 的镜像：一条入流进来，按 `tDest` 把每一拍（进而每一帧）分发到 `NUM_MASTERS_G` 条出流之一。它是实现「单物理通道承载多虚拟通道」接收侧的标准件。

它支持三种解码模式：

- **INDEXED**：把 `tDest` 的某几位直接当成输出端口号。例如 `tDest[1:0]=2` 就送到 2 号输出。最简单，适合端口号连续编号的场景。
- **ROUTED**：用一张编译期掩码表 `TDEST_ROUTES_G`，每条表项是一个 8 位模式串（含 `'0'`/`'1'`/`'-'`），用 `std_match` 匹配。适合「按地址段分发」，可灵活表达通配。
- **DYNAMIC**：与 ROUTED 类似，但掩码表由运行时端口 `dynamicRouteMasks`/`dynamicRouteDests` 提供，可在工作中改表。适合路由规则需要软件动态配置的场景。

#### 4.2.2 核心流程

```
每拍 (comb 进程):
  默认 入流 tReady=0; 各输出若被下游接收则 tValid 清 0

  解码 idx:                            # 决定这一拍该送到哪个输出
    INDEXED  -> idx = tDest[TDEST_HIGH: TDEST_LOW]
    ROUTED   -> 遍历表，第一个 std_match 命中的表项号
    DYNAMIC  -> 遍历表，第一个 (tDest & mask)==(dest & mask) 命中

  if idx 越界 (无人认领):
      入流 tReady=1                       # 吞掉，丢弃数据（blow off）
  elif 该输出当前空 且 入流有效:
      入流 tReady=1
      把入流整拍拷到 masters[idx]
```

注意「越界即丢」是一个重要行为：没有匹配的输出时，DeMux 不会卡住整条流，而是把这一拍当垃圾吞掉（`tReady=1` 但不送到任何输出）。这避免了无匹配地址导致上游死锁。

#### 4.2.3 源码精读

实体与泛型见 [axi/axi-stream/rtl/AxiStreamDeMux.vhd:26-50](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamDeMux.vhd#L26-L50)。三种模式的解码逻辑是全模块的「大脑」：

```vhdl
if (MODE_G = "INDEXED") then
   idx := to_integer(unsigned(sAxisMaster.tDest(TDEST_HIGH_G downto TDEST_LOW_G)));
elsif (MODE_G = "ROUTED") then
   idx := NUM_MASTERS_G;                         -- 先置为「无效」
   for i in 0 to NUM_MASTERS_G-1 loop
      if (std_match(sAxisMaster.tDest, TDEST_ROUTES_G(i))) then
         idx := i; exit;                          -- 升序取第一个命中
      end if;
   end loop;
elsif (MODE_G = "DYNAMIC") then
   idx := NUM_MASTERS_G;
   for i in 0 to NUM_MASTERS_G-1 loop
      if ((sAxisMaster.tDest and dynamicRouteMasks(i)) =
          (dynamicRouteDests(i) and dynamicRouteMasks(i))) then
         idx := i; exit;
      end if;
   end loop;
end if;
```

见 [axi/axi-stream/rtl/AxiStreamDeMux.vhd:102-125](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamDeMux.vhd#L102-L125)。注意 ROUTED/DYNAMIC 都先置 `idx := NUM_MASTERS_G`（一个合法范围外的值），若循环没命中就保持这个「无效」值。

「越界即丢」与正常搬运的分支：

```vhdl
if idx >= NUM_MASTERS_G then
   -- Blow off the data
   v.slave.tReady := '1';
elsif (v.masters(idx).tValid = '0') and (sAxisMaster.tValid = '1') then
   v.slave.tReady := '1';
   v.masters(idx) := sAxisMaster;          -- 整拍拷贝（含 tDest/tId/tUser）
end if;
```

见 [axi/axi-stream/rtl/AxiStreamDeMux.vhd:127-137](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamDeMux.vhd#L127-L137)。注意第二个分支要求 `v.masters(idx).tValid = '0'`——即目标输出此刻为空才能写入。若目标输出被下游压住（`tValid` 还没被 `tReady` 消化），DeMux 不会把入流 `tReady` 拉高，自然形成对上游的反压，且不会把数据覆盖到忙碌的输出上。

每个输出再各自挂一个 `AxiStreamPipeline`（见 [axi/axi-stream/rtl/AxiStreamDeMux.vhd:155-171](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamDeMux.vhd#L155-L171)），与 Mux 末级单条 Pipeline 对称——这是 SURF 的惯例：模块出口总是带可选流水，方便用时松时序。

实体开头的三条 `assert`（[axi/axi-stream/rtl/AxiStreamDeMux.vhd:71-82](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamDeMux.vhd#L71-L82)）在 elaboration 阶段就拦截配置错误：`MODE_G` 非法、INDEXED 模式下 `tDest` 位数不够装下 `NUM_MASTERS_G`、ROUTED 模式下表长不等于 `NUM_MASTERS_G`。这是「尽早失败」的防御式写法。

#### 4.2.4 代码实践

**实践目标**：验证 INDEXED 模式下「`tDest` 决定输出端口」与「无匹配时数据被丢弃」，并与 [4.1](#41-流复用仲裁axistreammux) 的 Mux 形成对照。

**操作步骤**（基于 `tests/axi/axi_stream/test_AxiStreamDeMux.py`）：

1. 生成源缓存后运行 DeMux 回归：
   ```bash
   ./.venv/bin/python -m pytest -q tests/axi/axi_stream/test_AxiStreamDeMux.py
   ```
2. 阅读 `dynamic_drop_reset` 场景（[test_AxiStreamDeMux.py:229-283](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/tests/axi/axi_stream/test_AxiStreamDeMux.py#L229-L283)）：它发送一个 `tDest=0xE3` 的帧，而动态表只匹配 `0xA0` 与 `0x50` 两段，因此该帧应被丢弃——断言 `not tb.any_output_valid()`。

**需要观察的现象**：

- `indexed_routing` 场景中，`tdest=0x00` 的帧只出现在 0 号输出、`tdest=0x01` 的帧只出现在 1 号输出，且 `tId`/`tUser` 等元数据端到端不变。
- `dynamic_drop_reset` 场景中，无匹配的帧被静默吞掉，两个输出都没有任何 `tValid`。

**预期结果**：3 个参数用例（`indexed_sync`、`routed_backpressure_sync`、`dynamic_drop_async_active_low`）全部 `PASSED`。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：在 INDEXED 模式下，若 `NUM_MASTERS_G=4`、`TDEST_LOW_G=0`、`TDEST_HIGH_G=0`（只用 `tDest` 的 1 位），会怎样？

**参考答案**：`TDEST_HIGH_G - TDEST_LOW_G + 1 = 1`，但 `log2(NUM_MASTERS_G) = log2(4) = 2`。模块开头的 `assert` 会触发并报错：`TDest range 0 downto 0 is too small for NUM_MASTERS_G=4`，elaboration 失败。必须把 `TDEST_HIGH_G` 提到 1（用 2 位）才能编址 4 个输出。

**练习 2**：为什么 DeMux 在「目标输出正忙」时不把数据写到其他空闲输出，而是反压整个入流？

**参考答案**：因为帧是「整帧属于同一个 `tDest`」的。一帧的第 N 拍送到输出 `idx`，第 N+1 拍也必须送到同一个 `idx`，否则帧会被拆碎到不同输出。所以当目标输出 `idx` 忙时，唯一的正确做法是反压入流（`tReady=0`），等该输出腾空再继续——这是保证帧完整性的必要代价。

### 4.3 Gearbox 位宽变换：AxiStreamGearbox 与 AxiStreamGearboxPack

#### 4.3.1 概念说明

当一条流的字宽需要在上下游之间改变（例如 PHY 侧 64 位、协议核侧 32 位；或把 16 位有效载荷塞进 8 字节总线），就需要位宽变换。SURF 提供两个层次：

- **`AxiStreamGearbox`**：通用位宽变换。它先判断两侧字节宽是否互为整数倍；若是，就委托给更省 LUT 的 `AxiStreamResize`（见 [u4-l2](u4-l2-axistream-fifo-pipeline.md)）；若不是，才启用自己那套「移位寄存器」实现。它还提供一个旁路 `FORCE_GEARBOX_IMPL_G`，强制走移位实现（用于 `tKeep` 打包等 `AxiStreamResize` 不支持的场景）。
- **`AxiStreamGearboxPack`**：一种特殊的「抽取子字段再打包」。它不是简单的宽窄变换，而是从每拍 `tData` 中取出 `[RANGE_HIGH_G:RANGE_LOW_G]` 这段连续位，按小端顺序逐段塞进输出流的满宽字里，配合 SSI 的 SOF/EOF 帧边界。常用于把「每拍只有少量有效位」的稀疏流压紧成「每拍都满」的密实流。

两者都是小端对齐，且都警告：**不要在交错 `tDest` 的流上使用**。

#### 4.3.2 核心流程

**AxiStreamGearbox（移位实现）的核心是一根「可变有效宽度」的移位寄存器**。设宽侧字宽 `MAX_C`、窄侧 `MIN_C`，则移位缓冲宽度为：

\[ \text{SHIFT\_WIDTH\_C} = \mathrm{wordCount}(\text{MAX\_C}, \text{MIN\_C}) \times \text{MIN\_C} + \text{MIN\_C} \]

其中 `wordCount` 给出「凑齐一个宽字所需窄字数」加一的余量缓冲。一个 `writeIndex` 指针记录缓冲里已写入了多少字节。每拍做两件事：

```
若输出侧空 (v.tValid=0):
   若 writeIndex >= 一个输出字 (MST_BYTES_C):
       把缓冲整体左移 MST_BYTES_C 字节（高位补 0），writeIndex -= MST_BYTES_C
       若移位后仍够一个字 -> 输出 tValid=1

若入流有效 且 输出空 且 未到帧尾:
   把入流字节按 writeIndex 偏移塞进缓冲
   writeIndex += 本拍有效字节数 (由 getTKeep(tKeep) 得到)
   若 writeIndex 够一个输出字 或 入流是 tLast:
       输出 tValid=1
       处理「跨两拍才结束」的尾部 (tLastDly)
```

关键点是 `writeIndex` 的增减完全跟着**有效字节数**（`tKeep` 解码出的连续有效字节）走，而不是固定字宽。这样无论 `tKeep` 模式是 NORMAL/COMP/COUNT，都能正确搬运「最后一拍不满」的尾部。

**AxiStreamGearboxPack 的核心是一个 `STREAM_WIDTH_C*2` 位的双倍缓冲**，每来一拍非 SOF 数据，就把它的 `[RANGE_HIGH:RANGE_LOW]` 子字段写到缓冲里一个预计算的索引处，整体左移一个字宽，逐步把稀疏字段压紧成满字。

#### 4.3.3 源码精读

**AxiStreamGearbox** 先做编译期分流。`WORD_MULTIPLE_C` 判断两侧字节宽是否互为整数倍：

```vhdl
constant WORD_MULTIPLE_C : boolean := (SLV_BYTES_C >= MST_BYTES_C and SLV_BYTES_C mod MST_BYTES_C = 0)
                                      or (MST_BYTES_C >= SLV_BYTES_C and MST_BYTES_C mod SLV_BYTES_C = 0);
```

见 [axi/axi-stream/rtl/AxiStreamGearbox.vhd:61-62](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamGearbox.vhd#L61-L62)。随后两个 generate 互斥分流：`GEN_RESIZE`（整数倍且未强制）例化 `AxiStreamResize`，`GEN_GEARBOX`（非整数倍或强制）走移位实现（见 [axi/axi-stream/rtl/AxiStreamGearbox.vhd:130-159](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamGearbox.vhd#L130-L159)）。注释点明动机：「Use AxiStreamResize if word multiple because less LUTs」（整数倍时用 Resize 更省资源）。

移位缓冲的宽度与寄存器定义：

```vhdl
constant SHIFT_WIDTH_C : positive := wordCount(MAX_C, MIN_C) * MIN_C + MIN_C;

type RegType is record
   writeIndex : natural range 0 to SHIFT_WIDTH_C-1;
   tValid     : sl;
   tData      : slv(8*SHIFT_WIDTH_C-1 downto 0);
   tKeep      : slv(1*SHIFT_WIDTH_C-1 downto 0);
   ...
```

见 [axi/axi-stream/rtl/AxiStreamGearbox.vhd:77-92](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamGearbox.vhd#L77-L92)。注意 `tData`、`tKeep`、`tStrb`、`tUser` 都是按 `SHIFT_WIDTH_C` 字节宽度声明的，也就是说侧带和数据一起被移位，保证字节级对齐。

移位（窄化输出）的核心——把缓冲左移 `MST_BYTES_C` 字节、高位补零：

```vhdl
if (v.writeIndex >= MST_BYTES_C) then
   v.writeIndex := v.writeIndex - MST_BYTES_C;
   v.tData := slvZero(8*MST_BYTES_C) & r.tData(8*SHIFT_WIDTH_C-1 downto 8*MST_BYTES_C);
   ...
   v.tKeep := slvZero(1*MST_BYTES_C) & r.tKeep(1*SHIFT_WIDTH_C-1 downto 1*MST_BYTES_C);
```

见 [axi/axi-stream/rtl/AxiStreamGearbox.vhd:192-228](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamGearbox.vhd#L192-L228)。

接收新数据并按 `writeIndex` 偏移写入，随后用 `getTKeep` 推进指针：

```vhdl
for i in (SLV_BYTES_C*8)-1 downto 0 loop
   v.tData((8*v.writeIndex)+i) := sAxisMaster.tData(i);   -- 逐位赋值（兼容 ASIC 流程）
end loop;
...
v.writeIndex := v.writeIndex + getTKeep(resize(sAxisMaster.tKeep(...), ...), SLAVE_AXI_CONFIG_G);
```

见 [axi/axi-stream/rtl/AxiStreamGearbox.vhd:232-304](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamGearbox.vhd#L232-L304)。这里有两点值得注意：(1) 用 `for` 循环逐位赋值而非整段切片，注释说是「to appease ASIC synthesis flow tools」——某些 ASIC 综合工具对跨范围切片赋值处理不佳；(2) `writeIndex` 增量取自 `getTKeep`，即真正有效的字节数，因此能正确处理任意 `tKeep` 模式的尾部。

实体开头的 `assert` 强制：宽→窄变换时必须 `READY_EN_G=true`（见 [axi/axi-stream/rtl/AxiStreamGearbox.vhd:118-119](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamGearbox.vhd#L118-L119)），因为一个宽字要拆成多拍窄字输出，期间必须能反压上游。

**AxiStreamGearboxPack** 的编译期常量与索引预计算。`SIZE_DIFFERENCE_C = STREAM_WIDTH_C - PACK_SIZE_C` 是「每塞一段能省下的位数」；`computeIndices` 预先算好每一段在双倍缓冲里的写入位置（注释指出 Vivado 无法在 comb 进程里现场算这些索引）：

```vhdl
constant SIZE_DIFFERENCE_C : integer := STREAM_WIDTH_C-PACK_SIZE_C;
function computeIndices return IntegerArray is ...  -- ret(i) = STREAM_WIDTH_C - i*SIZE_DIFFERENCE_C
constant ASSIGNMENT_INDECIES_C : IntegerArray := computeIndices;
```

见 [axi/axi-stream/rtl/AxiStreamGearboxPack.vhd:48-63](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamGearboxPack.vhd#L48-L63)。

打包主体：SOF 帧头原样直通，其余每拍把子字段写到双倍缓冲的预计算索引处，整体左移一个字宽，并按 `index` 决定何时输出 `valid`：

```vhdl
if (rawSsiMaster.sof = '1') then
   v.data(STREAM_WIDTH_C-1 downto 0) := rawSsiMaster.data(STREAM_WIDTH_C-1 downto 0);
   v.packedSsiMaster.valid := '1';  v.packedSsiMaster.sof := '1';
   v.index := (others => '0');
else
   v.data(STREAM_WIDTH_C-1 downto 0) := r.data(STREAM_WIDTH_C*2-1 downto STREAM_WIDTH_C);  -- 左移一字
   indexInt := ASSIGNMENT_INDECIES_C(conv_integer(r.index));
   v.data(indexInt+PACK_SIZE_C-1 downto indexInt) := rawSsiMaster.data(RANGE_HIGH_G downto RANGE_LOW_G);
   v.packedSsiMaster.valid := toSl(r.index /= 0) or rawSsiMaster.eofe or rawSsiMaster.eof;
   v.index := r.index + 1;
end if;
```

见 [axi/axi-stream/rtl/AxiStreamGearboxPack.vhd:106-135](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamGearboxPack.vhd#L106-L135)。注意它通过 `axis2ssiMaster`/`ssi2AxisMaster` 在 AXI-Stream 与 SSI 之间转换，用 SSI 的 `sof/eof/eofe` 来界定帧——这预告了 [u5-l1](u5-l1-ssi-sideband-framing.md) 将讲的 SSI 侧带。

#### 4.3.4 代码实践

**实践目标**：直观感受 Gearbox「整数倍走 Resize、非整数倍走移位」的分流，以及 `writeIndex` 如何跟随 `tKeep`。

**操作步骤**（源码阅读型实践）：

1. 运行 Gearbox 与 Pack 的回归测试，确认行为：
   ```bash
   ./.venv/bin/python -m pytest -q tests/axi/axi_stream/test_AxiStreamGearbox.py \
     tests/axi/axi_stream/test_AxiStreamGearboxPack.py \
     tests/axi/axi_stream/test_AxiStreamGearboxUnpack.py
   ```
2. 在 `AxiStreamGearbox.vhd` 中定位 `GEN_RESIZE` 与 `GEN_GEARBOX` 两个 generate（[:130 与 :159](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamGearbox.vhd#L130-L159)）。
3. 做一个思想实验（**示例代码**，非项目原有）：若 `SLAVE_AXI_CONFIG_G.TDATA_BYTES_C=4`、`MASTER_AXI_CONFIG_G.TDATA_BYTES_C=3`（4 字节→3 字节，非整数倍），手算 `MAX_C=4`、`MIN_C=3`，`wordCount(4,3)=2`，故 `SHIFT_WIDTH_C = 2*3+3 = 9` 字节，缓冲为 72 位。

**需要观察的现象**：第 3 步中，缓冲比单个输出字（3 字节）宽得多，正是因为非整数倍变换需要「积攒多个输入字才能凑出若干个输出字」，且要留余量处理尾部。

**预期结果**：你能解释为什么非整数倍变换必须用移位缓冲、而整数倍（如 4→8）可以直接拼接无需缓冲。**待本地验证**：pytest 的具体用例数与通过情况。

#### 4.3.5 小练习与答案

**练习 1**：把一个 8 字节（64 位）的流变成 4 字节（32 位），会走 `GEN_RESIZE` 还是 `GEN_GEARBOX`？为什么？

**参考答案**：走 `GEN_RESIZE`（即委托 `AxiStreamResize`）。因为 `SLV_BYTES_C=8`、`MST_BYTES_C=4`，`8 mod 4 = 0`，满足 `WORD_MULTIPLE_C` 为真，且未设 `FORCE_GEARBOX_IMPL_G`。整数倍关系下无需移位缓冲，直接拆分即可，且更省 LUT。

**练习 2**：`AxiStreamGearbox` 的注释写「Re-sizing is always little endian」且「should not be used when interleaving tDests」。请结合 `writeIndex` 解释后者。

**参考答案**：移位缓冲是一根连续的、按字节顺序排列的移位寄存器，它假设「前后拍的数据在字节流上是连续递进」的。若流在交错 `tDest`（即相邻拍可能属于不同虚拟通道、不能视为同一条字节流），那么把不同 `tDest` 的字节拼进同一个缓冲会破坏各自的帧边界与字节序。因此 Gearbox 只适用于单一字节流（单 `tDest`）的宽窄变换。

## 5. 综合实践

设计一个「DeMux → Mux 回环」的小系统，把本讲三个模块（Mux、DeMux 的 INDEXED 模式，以及位宽概念）串起来。

**任务**：一条 32 位（4 字节）AXI-Stream 入流，每帧带不同的 `tDest`（取值 0~3）。先用一个 `NUM_MASTERS_G=4` 的 DeMux 按 `tDest[1:0]` 分到 4 路；再用一个 `NUM_SLAVES_G=4` 的 Mux 把 4 路合回一条流，Mux 工作在 INDEXED 模式（`TDEST_LOW_G=0`）。

**要求你推理并回答**（这是源码阅读型实践，**示例代码**仅作示意，非项目原有文件）：

1. **配置一致性**：DeMux 的 `TDEST_HIGH_G` 应取多少？Mux 的 `DEST_SIZE_C` 是多少？两者的 `tDest` 位段能否对齐？
2. **帧序保持**：Mux 默认按帧原子仲裁（`ILEAVE_EN_G=false`）。一帧从输入到回环输出，其 `tDest` 值是否等于原值？帧内字节顺序是否被打乱？
3. **回压传递**：若 Mux 输出端被下游压住，反压如何经 Mux → 某 1 路 → DeMux → 回到原始入流？会不会影响其他 3 路？

**示例 VHDL 结构**（仅示意实例化接线，省略时钟复位与记录赋值）：

```vhdl
-- 示例代码：DeMux -> Mux 回环（仅示意，非项目原有文件）
U_DeMux : entity surf.AxiStreamDeMux
   generic map (
      NUM_MASTERS_G => 4,
      MODE_G        => "INDEXED",
      TDEST_HIGH_G  => 1,        -- 用 tDest[1:0] 编址 4 个输出
      TDEST_LOW_G   => 0)
   port map (
      axisClk      => axisClk,
      axisRst      => axisRst,
      sAxisMaster  => inMaster,  sAxisSlave  => inSlave,
      mAxisMasters => demuxOut,  mAxisSlaves => demuxOutSlave);

U_Mux : entity surf.AxiStreamMux
   generic map (
      NUM_SLAVES_G => 4,
      MODE_G       => "INDEXED",  -- 把源号写回 tDest[1:0]
      TDEST_LOW_G  => 0)
   port map (
      axisClk      => axisClk,
      axisRst      => axisRst,
      sAxisMasters => demuxOut,   sAxisSlaves => demuxOutSlave,
      mAxisMaster  => outMaster,  mAxisSlave  => outSlave);
```

**参考答案**：

1. DeMux 需 `TDEST_HIGH_G=1`、`TDEST_LOW_G=0`（2 位编址 4 个输出）。Mux 的 `DEST_SIZE_C = bitSize(4-1) = 2`，把源号写进 `tDest[1:0]`。由于 DeMux 输出的 `tDest` 低 2 位恰好就是该输出端口号，而 Mux 把源号也写进同样的 `tDest[1:0]`，回环后 `tDest` 低 2 位等于原值——**位段对齐**。
2. **帧序与 `tDest` 保持**：因为 DeMux 把整帧（直到 `tLast`）都送到同一个输出，Mux 又按帧原子地取完一帧再切换源，所以一帧的字节顺序不被打乱，且 `tDest` 经 Mux 的 INDEXED 改写后恰好还原为原端口号。唯一要注意：Mux 会把 `tDest` 的高位（第 2~7 位）清零（见 [AxiStreamMux.vhd:235-238](https://github.com/slaclab/surf/blob/0ca723d282315cceb93f374ab69284db3d3d9d83/axi/axi-stream/rtl/AxiStreamMux.vhd#L235-L238)），所以若原帧 `tDest` 高位非零，回环后高位会丢失。
3. **反压隔离**：Mux 被压住时，它当前锁定的那一路 `tReady` 被拉低；由于 Mux 一帧内只服务一路，反压只经「当前正在传输的那一路」传到 DeMux 的对应输出，再经 DeMux 反压到原始入流。其余 3 路此刻本就没有数据在传（Mux 没选中它们），所以反压不会跨帧影响别的 `tDest`——这正是按帧仲裁带来的隔离性。

> 注：仓库没有「DeMux→Mux 回环」现成测试，上述推理需结合 [4.1](#41-流复用仲裁axistreammux)、[4.2](#42-tdest-分发axistreamdemux) 的源码自行验证；若要实测，可仿照 `test_AxiStreamDeMux.py` 写一个包装了上述两个实例的 IP integrator 顶层再跑 cocotb。

## 6. 本讲小结

- `AxiStreamMux` 用「锁定式轮询仲裁 + 遇 `tLast` 释放」实现多入单出，默认按帧原子切换，保证帧完整；`INDEXED` 模式会把源号写进 `tDest`。
- `AxiStreamDeMux` 是其镜像，按 `tDest` 单入多出，支持 INDEXED（直接当端口号）、ROUTED（`std_match` 通配表）、DYNAMIC（运行时改表）三种解码；无匹配的 `tDest` 会被静默丢弃以防上游死锁。
- 仲裁向量位宽 `ARB_BITS_C` 取 2 的幂，源于 `arbitrate`/`priorityEncode` 用循环移位实现公平轮询；`PRIORITY_G` 与 `disableSel` 提供静态/动态的优先级与屏蔽。
- DeMux 对忙碌的目标输出会反压整个入流（而非改投别处），这是帧完整性的必要代价。
- `AxiStreamGearbox` 先按「字宽是否整数倍」分流：整数倍走更省资源的 `AxiStreamResize`，非整数倍才用自带移位缓冲；`writeIndex` 跟随 `getTKeep` 推进，能正确处理任意 `tKeep` 模式与帧尾。
- `AxiStreamGearboxPack` 是「抽取子字段再压紧」的特殊打包，用双倍缓冲与 SSI 的 SOF/EOF 界帧；两个 Gearbox 都只适用于单一 `tDest` 的字节流，不可用于交错 `tDest`。

## 7. 下一步学习建议

- 本讲的 Mux/DeMux 都围绕 `tDest`，而 SSI 在 `tDest` 之上又叠加了 SOF/EOF/EOFE 等帧侧带——下一讲 [u5-l1（SSI 侧带与帧边界）](u5-l1-ssi-sideband-framing.md) 将把 `tDest` 升级为「虚拟通道（VC）」并讲清帧边界如何编码进 `tUser`。
- 若你想把一条流按 `tDest` 分发后还做流量监控，可继续读 [u4-l4（AXI-Stream 监控、抽头与测试码型）](u4-l4-axistream-monitoring.md) 的 `AxiStreamMon`/`AxiStreamTap`。
- 想亲手验证本讲的回环实践，建议先读 [u9-l1（cocotb 测试工具链）](u9-l1-cocotb-toolchain.md) 与 [u9-l2（编写一个 cocotb 回归测试）](u9-l2-writing-cocotb-test.md)，再仿照 `test_AxiStreamDeMux.py` 的方法学头与参数扫描写自己的回环测试。

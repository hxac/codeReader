# AXI-Lite 子系统

## 1. 本讲目标

本讲承接 u5-l2（AXI 交叉栏、节流与流水线），把总线从「完整 AXI4」收窄到它的轻量子集 **AXI4-Lite**。读完本讲，你应当能够：

- 说清 AXI-Lite 与完整 AXI 在信号、突发、吞吐上的差别，以及为什么它最适合做「寄存器总线」。
- 读懂 `axi_lite_pkg` 如何用 record 把五条通道的信号分层聚合，并用打包函数把它们压成最窄向量。
- 解释 `axi_to_axi_lite` 如何把一条完整 AXI 总线「降级」成 AXI-Lite：限制突发长度、节流到单事务在途、保存 ID 回送、对非法事务返回 `SLVERR`。
- 理解 `axi_lite_mux`（1-to-N 地址分发，带 `DECERR` 兜底）与 `axi_lite_simple_read/write_crossbar`（N-to-1 汇聚，复用 AXI 交叉栏）这两件相反方向的小工具。
- 认识 `axi_lite_cdc`（按通道挂异步 FIFO 跨域）与 `axi_lite_pipeline`（按通道挂 `handshake_pipeline`）如何作为「胶水」把上述积木拼成一个完整子系统。

本讲只覆盖 `axi_lite` 模块；寄存器文件本身（`axi_lite_register_file`）留给 u6-l1。

## 2. 前置知识

在进入源码前，先建立两条直觉。

**第一，AXI-Lite 是 AXI4 的「单拍寄存器版」。** 完整 AXI4（见 u5-l2）支持 1–256 拍的突发（burst）、可变位宽的 `size`、多事务在途（outstanding）、ID 与缓存属性等丰富信号。AXI-Lite 把这些都砍掉：

- 每次事务**只有一拍数据**（突发长度恒为 1）。
- 数据位宽**只能是 32 或 64**（这是协议硬性要求，本项目的 `sanity_check_axi_lite_data_width` 会断言）。
- 没有 `len`、`size`、`burst`、`cache`、`id`（协议层不传 ID）等信号，只有「地址 + 数据 + 字节使能 + 响应」。

因此 AXI-Lite 的典型用途是 **CPU 访问片上寄存器**——一次读/写一个 32/64 位字，带宽要求极低，但握手必须标准、必须能挂到标准总线上。

**第二，握手规则与 AXI 完全一致。** `valid` 不得组合依赖 `ready`，`ready` 可以组合依赖 `valid`；`valid && ready` 同拍为高才完成一次 beat。这一点 AXI-Lite 与 AXI、与 u2-l1 的 AXI-Stream 式约定是一脉相承的，所以本讲不再重复证明，只关注「Lite 子系统」特有的结构。

> 术语提示：**m2s** = master-to-slave（主到从），**s2m** = slave-to-master（从到主）；**AR**=读地址，**R**=读数据，**AW**=写地址，**W**=写数据，**B**=写响应。AXI-Lite 同样有这五条通道。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [modules/axi_lite/src/axi_lite_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_pkg.vhd) | AXI-Lite 的 record 类型定义、常量（响应码、位宽范围）与打包/解包函数。全模块的「公共词汇表」。 |
| [modules/axi_lite/src/axi_to_axi_lite.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_to_axi_lite.vhd) | 协议转换器：把完整 AXI 总线降级为 AXI-Lite，并节流到单事务在途。 |
| [modules/axi_lite/src/axi_lite_mux.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_mux.vhd) | 1-to-N 地址解码分发器（一个 master 到 N 个 slave），无匹配地址时回 `DECERR`。 |
| [modules/axi_lite/src/axi_lite_simple_read_crossbar.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_simple_read_crossbar.vhd) | N-to-1 读交叉栏薄壳，复用 AXI 模块的 `axi_simple_read_crossbar`。 |
| [modules/axi_lite/src/axi_lite_cdc.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_cdc.vhd) | 跨时钟域：给五条通道各挂一个异步 FIFO。 |
| [modules/axi_lite/src/axi_lite_pipeline.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_pipeline.vhd) | 流水线：给五条通道各挂一个 `handshake_pipeline`，改善时序。 |

另外，`modules/axi_lite/src/axi_lite_simple_write_crossbar.vhd` 是读交叉栏的写侧孪生体，结构与读版完全对称，本讲在读版基础上点出差异即可。

## 4. 核心概念与源码讲解

### 4.1 AXI-Lite 协议要点与 axi_lite_pkg 类型定义

#### 4.1.1 概念说明

`axi_lite_pkg` 是整个 Lite 子系统的「公共词汇表」。它本身**不描述任何电路**，只是把 AXI-Lite 协议里的信号用 VHDL 的 `record`（记录类型）固化下来，并配上把这些记录压平成向量的函数。这与 u5-l1 讲过的 `axi_stream_pkg`、u5-l2 讲过的 `axi_pkg` 思路完全一致——hdl-modules 偏好「用 record 把一组相关信号捆在一起，端口只声明一两个记录对象」，从而让模块端口整洁、不易接错线。

设计要点（贯穿全项目，承接 u5-l1/u5-l2）：

1. **字段按最大位宽声明，打包时只取实际用到的位。** 例如地址字段声明为 64 位宽，但用户只用了 24 位地址时，FIFO 只搬运那 24 位。
2. **`valid`/`ready` 控制位不进打包向量。** 它们由 FIFO/流水线的握手通路重新产生，不占数据存储。
3. **「排除」一些协议里有但本项目几乎不用的信号**（如 `prot`），并显式注释「Excluded member」。

#### 4.1.2 核心流程

五条通道各自的 m2s/s2m 记录 → 两两聚合成「读总线」「写总线」→ 再聚合成完整 AXI-Lite 总线：

```
m2s_a_t (valid, addr)          s2m_a_t (ready)         -- AR / AW 通道
m2s_w_t (valid, data, strb)    s2m_w_t (ready)         -- W 通道
m2s_b_t (ready)                s2m_b_t (valid, resp)   -- B 通道
m2s_r_t (ready)                s2m_r_t (valid, data, resp) -- R 通道
        │                          │
        ▼                          ▼
axi_lite_read_m2s_t (ar, r)    axi_lite_read_s2m_t (ar, r)
axi_lite_write_m2s_t (aw, w, b) axi_lite_write_s2m_t (aw, w, b)
        │                          │
        ▼                          ▼
axi_lite_m2s_t (read, write)   axi_lite_s2m_t (read, write)
```

响应码是 2 位的 `axi_lite_resp_t`，共四种取值，本项目用命名常量表达，避免到处写魔法数字。

#### 4.1.3 源码精读

地址位宽被限定在 1–64，注意这是「最大值」语义——实现时只取实际用到的位：

[modules/axi_lite/src/axi_lite_pkg.vhd:28-28](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_pkg.vhd#L28-L28) 定义地址位宽上限为 64。

读/写地址通道的主到从记录只保留 `valid` 和 `addr`，显式排除 `prot`：

[modules/axi_lite/src/axi_lite_pkg.vhd:31-36](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_pkg.vhd#L31-L36) 定义 `axi_lite_m2s_a_t`，并注释「Excluded members: prot — typically not changed on a transfer-to-transfer basis」。

数据位宽的合法性被强约束为 32 或 64，这是 AXI-Lite 协议本身的要求；`sanity_check` 用 `report ... severity failure` 在精化期把非法值挡在综合前：

[modules/axi_lite/src/axi_lite_pkg.vhd:248-257](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_pkg.vhd#L248-L257) 断言 `data_width` 必须是 32 或 64，否则报错返回 `false`。

响应码用四个命名常量表达，与 AXI 协议一致（OKAY=00, EXOKAY=01, SLVERR=10, DECERR=11）：

[modules/axi_lite/src/axi_lite_pkg.vhd:114-117](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_pkg.vhd#L114-L117) 定义四种响应码常量，下游模块直接引用 `axi_lite_resp_decerr` 等。

`to_slv` 把 W 通道记录压成最窄向量：先放 `data`，再放 `strb`，**排除 `valid`**（由 FIFO 的握手重新产生）。`hi = result'high` 的断言确保打包宽度与 `_sz` 函数算出的宽度严格一致：

[modules/axi_lite/src/axi_lite_pkg.vhd:292-309](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_pkg.vhd#L292-L309) `to_slv` 把 `axi_lite_m2s_w_t` 打包为 `data_width + strb_width` 位的向量，末尾 `assert hi = result'high` 守护位宽一致性。

最后三层聚合得到完整总线记录，并配套 `*_vec_t` 数组类型（供 mux/交叉栏表达多路端口）：

[modules/axi_lite/src/axi_lite_pkg.vhd:213-233](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_pkg.vhd#L213-L233) 定义 `axi_lite_m2s_t`/`axi_lite_s2m_t` 完整总线记录及其初值常量。

#### 4.1.4 代码实践

**实践目标**：验证打包/解包函数的位宽守恒与数据往返正确性，并体会「`valid` 被排除」带来的位宽节省。

**操作步骤**：

1. 打开 `modules/axi_lite/test/tb_axi_lite_pkg.vhd`，找到 `test_slv_conversion` 这个测试用例。
2. 阅读 `module_axi_lite.py` 的 `setup_vunit`，确认它为 `data_width` 取 32 和 64 两组值各登记一次配置。
3. 在本地运行该单测（待本地验证）：

   ```bash
   python tools/simulate.py axi_lite.tb_axi_lite_pkg
   ```

**需要观察的现象**：对 `data_width=32`，W 通道打包宽度应为 \(32 + 32/8 = 36\) 位；对 `data_width=64`，应为 \(64 + 64/8 = 72\) 位；`valid` 不在打包结果中。

**预期结果**：测试通过；如果断言 `hi = result'high` 失败，说明 `_sz` 函数与 `to_slv` 的字段布局不一致（本项目里这两者必须严格对齐）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `axi_lite_m2s_a_t` 要排除 `prot` 信号？

**参考答案**：注释已说明 `prot` 通常不会在事务之间变化，把它放进每次握手都要搬运的记录/向量里会无谓增加 FIFO 宽度与逻辑。AXI-Lite 的典型用法（CPU 配寄存器）几乎用不到 `prot`，所以本项目选择在类型层就省掉它，以节省资源——这正是 hdl-modules「面积优先」取向的体现。

**练习 2**：如果有人把 `data_width` 设为 16，综合前会发生什么？

**参考答案**：`sanity_check_axi_lite_data_width` 会在精化期执行 `report ... severity failure`，直接终止 elaboration。错误被挡在综合之前，不会产生错误的硬件。

---

### 4.2 axi_to_axi_lite：把完整 AXI 降级为 AXI-Lite

#### 4.2.1 概念说明

很多标准 IP（CPU 核、PCIe 控制器）对外给出的是**完整 AXI4** 总线，而本项目里大量 slave（如 `axi_lite_register_file`）只讲 **AXI-Lite**。`axi_to_axi_lite` 就是两者之间的「翻译器」：一侧是完整 AXI 记录端口，另一侧是 AXI-Lite 记录端口，中间把协议降级。

它只接受「行为良好（well-behaved）」的 AXI 事务：

- **突发长度必须为 1**（即 AXI 的 `len = 0`，因为 `len` 是 0 基的）。
- **突发大小必须等于总线位宽**（即 `size = log2(data_width/8)`，整字传输）。

不满足这两个条件的事务，转换器**不会让总线挂死**，而是在响应通道返回 `SLVERR`（slave error）。这一点很重要——错误以标准响应的形式上报，master 能继续处理后续事务。

另外，转换器会**主动节流 AXI 侧**，保证「读、写各自最多只有一个 outstanding 事务在途」。注释里点出了一个现实原因：虽然 AXI-Lite 标准允许 outstanding，但某些 Xilinx 硬核（如 PCIe DMA bridge）与之配合不好。所以本项目选择保守地节流到单事务，换取更广的兼容性。

#### 4.2.2 核心流程

转换器要做四件事，对应代码里的几段逻辑：

```
1. 通道直连：地址/数据/strb 从 AXI 侧复制到 AXI-Lite 侧（截取实际位宽）
2. 节流到单事务：用 ar_done/aw_done/w_done 门控 valid 与 ready
3. 镜像 ID：在 AR/AW 握手时保存 AXI 的 id，在 R/B 响应时回送（AXI-Lite 无 id）
4. 错误检查：检查 len/size，非法则在 R/B 响应里返回 SLVERR
```

「节流到单事务」是关键。以读通道为例：当 AR 握手成功后置 `ar_done`，把 `ar.valid` 拉低（`... and not ar_done`）、把上游 `ar.ready` 也屏蔽，直到 R 响应被取走才清 `ar_done` 放行下一笔。这保证 AXI-Lite 侧同一时刻最多看到一笔读地址在途。

#### 4.2.3 源码精读

文件头注释明确说明了「只处理 well-behaved 事务」与「节流到单事务」两条契约：

[modules/axi_lite/src/axi_to_axi_lite.vhd:9-19](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_to_axi_lite.vhd#L9-L19) 注释声明 burst length 必须为 1、size 必须等于总线宽度，否则返回 `SLVERR`；并说明会节流到单事务在途以兼容 Xilinx PCIe DMA bridge。

合法 AXI 事务的判定标准在精化期算成常量。注意 `expected_len := 0` 对应「突发长度为 1」：

[modules/axi_lite/src/axi_to_axi_lite.vhd:54-55](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_to_axi_lite.vhd#L54-L55) 定义期望的 `len` 与 `size`，后续用它俩检查每笔事务。

读通道的节流与直连。`ar.valid` 与上游 `ar.ready` 都被 `not ar_done` 门控；R 响应恒为单拍（`last <= '1'`），`resp` 在出错时改写为 `SLVERR`，否则透传：

[modules/axi_lite/src/axi_to_axi_lite.vhd:75-86](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_to_axi_lite.vhd#L75-L86) 读通道：AR 用 `not ar_done` 节流，R 通道回送 `read_id`、`last` 恒 1、`resp` 出错时改写。

`mirror_id` 进程在 AR/AW 握手时锁存 AXI 的 ID，在 R/B 握手时清 `done` 标志放行下一笔。这是「AXI-Lite 无 ID，但 AXI 需要 ID 回送」的桥接：

[modules/axi_lite/src/axi_to_axi_lite.vhd:111-139](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_to_axi_lite.vhd#L111-L139) `mirror_id` 进程：保存 ID、置 `done`、响应完成后清 `done`，实现单事务节流与 ID 回送。

错误检查进程在 AR/AW 握手拍比对 `len`/`size`，非法则置 `error` 标志，供响应通道改写成 `SLVERR`：

[modules/axi_lite/src/axi_to_axi_lite.vhd:143-174](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_to_axi_lite.vhd#L143-L174) `check_for_bus_error` 进程：比对实际 `len`/`size` 与期望值，置 `read_error`/`write_error`。

#### 4.2.4 代码实践

**实践目标**：用 BFM 搭一个「AXI master → axi_to_axi_lite → AXI-Lite slave」回环，验证正常读写与非法突发的不同表现。

**操作步骤**：

1. 打开 [modules/axi_lite/test/tb_axi_to_axi_lite.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/test/tb_axi_to_axi_lite.vhd)。注意它的 DUT 实例化方式：BFM 的 `axi_master` 接 `axi_m2s`/`axi_s2m`，BFM 的 `axi_lite_slave` 接 `axi_lite_m2s`/`axi_lite_s2m`，中间夹的就是 `axi_to_axi_lite`。
2. 阅读它的 `read_write_data` 测试：用 `write_bus`/`read_bus` 各做 1000 次随机字读写，并用 `set_expected_word` + `check_equal` 校验。
3. 再打开 `tb_axi_to_axi_lite_bus_error.vhd`，看它如何故意发一笔非法突发并检查响应是否为 `SLVERR`。
4. 本地运行（待本地验证）：

   ```bash
   python tools/simulate.py axi_lite.tb_axi_to_axi_lite
   python tools/simulate.py axi_lite.tb_axi_to_axi_lite_bus_error
   ```

**需要观察的现象**：`read_write_data` 下数据逐字往返正确；`bus_error` 下响应通道的 `resp` 为 `SLVERR`（`"10"`），且 master 不挂死、能继续。

**预期结果**：两组测试均通过，证明协议降级正确、错误以标准响应形式上报。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `axi_s2m.read.r.last <= '1'` 可以写死为常数？

**参考答案**：因为转换器只允许突发长度为 1 的 AXI 事务通过，所以 AXI-Lite 侧的 R 通道永远只有一拍，那一拍自然就是最后一拍，`last` 恒为 1。合法事务经过降级后必为单拍。

**练习 2**：`read_id`/`write_id` 这两个寄存器解决了什么问题？

**参考答案**：AXI 协议要求响应携带与请求相同的 ID，而 AXI-Lite 协议层没有 ID 信号。转换器在请求握手时把 AXI 的 ID 锁存进寄存器，等 AXI-Lite 侧给出无 ID 的响应时，再把锁存的 ID 拼回去还给 AXI master，从而维持 AXI 侧的 ID 语义。

---

### 4.3 axi_lite_mux 与简单交叉栏：1-to-N 分发与 N-to-1 汇聚

#### 4.3.1 概念说明

一个 CPU（master）往往要访问**多个**寄存器块（slave）。`axi_lite_mux` 解决的是「**一主到多从**」的地址分发：master 给一个地址，mux 根据地址落在哪个基址区间，把它路由到对应的 slave 端口。

反过来，有时是**多个 master**（如两个 CPU 核）要抢**同一个** slave，这时用 `axi_lite_simple_read/write_crossbar`（N-to-1 汇聚）。这两个 Lite 交叉栏其实是**薄壳**——它们只把 AXI-Lite 信号映射到完整 AXI 记录上，真正仲裁逻辑复用 u5-l2 讲过的 `axi_simple_read/write_crossbar`。这种「不重写、复用已有实现」正是 hdl-modules 的一贯取向。

`axi_lite_mux` 有一个值得注意的工程细节：**地址不匹配任何 slave 时不会让 master 挂死**，而是用一个「虚拟的解码错误端口」把响应做成 `DECERR`，同时握手照常完成。这样 master 收到的是标准的解码错误响应，而不是死锁。

#### 4.3.2 核心流程

mux 的核心是两个独立的状态机（读、写各一个），每个都是「等待输入 → 等待完成」两态：

```
wait_for_input:
  读侧：用 decode(ar.addr, masks) 算出目标 slave 下标
       若 ar.valid 则拉高 let_ar_through/let_r_through，进入 wait_for_done
  写侧：同理用 aw.addr 解码，拉高 let_aw/let_w/let_b_through

wait_for_done:
  读侧：等 R 响应握手完成（let_r_through 被清）后回到 wait_for_input
  写侧：等 B 响应握手完成后回到 wait_for_input
```

「选中即锁定」：一旦根据地址选中某个 slave（或选中 `DECERR` 虚拟口），就锁住这个选择直到整笔事务（读：R 通道；写：B 通道）完成，期间不再解码新地址。这与 u5-l2 讲过的「simple 锁定式仲裁」同源。

为什么「等 B 完成就够」？源码注释援引了 **AXI 标准 A3.3.1**：slave 必须在 W 完成后才能拉 BVALID，而要完成 W 又必须先完成 AW，所以 B 通道握手完成隐含了 AW、W 都已完成的因果链，只需看 B 即可。读侧同理：AR 完成后才能 RVALID，所以看 R 即可。

`assign_m2s_vec` 进程用一个 `for` 循环给每个 slave 端口赋默认值（全部直连 master 信号），再把「非选中」端口的 `valid` 与 `ready` 用 `to_sl(read_slave_select = slave_idx)` 清零，从而只让被选中的那一路真正握手。

#### 4.3.3 源码精读

文件头说明了 `DECERR` 兜底机制——握手照常、只把响应改成解码错误：

[modules/axi_lite/src/axi_lite_mux.vhd:9-15](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_mux.vhd#L9-L15) 注释：地址不匹配时返回 `DECERR`，但握手正常完成，master 不会挂。

`slave_decode_error_idx` 取「上界 + 1」，作为一个「虚拟 slave 下标」专门表示解码错误：

[modules/axi_lite/src/axi_lite_mux.vhd:50-55](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_mux.vhd#L50-L55) 用 `calculate_mask` 预算地址掩码；定义解码错误下标为 slave 数量。

`assign_m2s_vec` 循环：默认把 master 信号扇出到所有 slave，再用 `to_sl(select = slave_idx)` 仅放行被选中那一路的控制位：

[modules/axi_lite/src/axi_lite_mux.vhd:124-147](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_mux.vhd#L124-L147) `for` 循环为每个 slave 端口做「默认直连 + 选中门控」。

读侧状态机：解码 → 放行 → 等 R 完成。注释解释了「只看 R 就够」的 AXI A3.3.1 因果依据，以及为何不为了省一拍而增加控制信号扇出（时序优先）：

[modules/axi_lite/src/axi_lite_mux.vhd:157-193](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_mux.vhd#L157-L193) `select_read_slave` 进程：两态机完成「解码→锁定→等待 R 完成」。

读侧 `assign_s2m_read` 里对解码错误口的处理：地址不匹配时，AR 给 `ready`、R 给 `valid + DECERR`，把响应捏成解码错误但握手完整：

[modules/axi_lite/src/axi_lite_mux.vhd:77-85](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_mux.vhd#L77-L85) 当 `read_slave_select = slave_decode_error_idx` 时，R 通道返回 `valid + DECERR`。

再看 N-to-1 的简单读交叉栏——它只是把 AXI-Lite 信号映射到 AXI 记录、硬塞 `r.last <= '1'`（Lite 恒单拍），再实例化 u5-l2 的 `axi_simple_read_crossbar`：

[modules/axi_lite/src/axi_lite_simple_read_crossbar.vhd:58-71](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_simple_read_crossbar.vhd#L58-L71) `for` 生成把每个 AXI-Lite 输入端口的 `ar.valid/addr`、`r.ready` 等映射到完整 AXI 记录。

[modules/axi_lite/src/axi_lite_simple_read_crossbar.vhd:83-100](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_simple_read_crossbar.vhd#L83-L100) 硬置 `r.last <= '1'`（AXI-Lite 恒单拍），并实例化复用的 `axi_simple_read_crossbar`。

#### 4.3.4 代码实践

**实践目标**：用 mux 把一个 master 接到两个 slave，验证地址路由正确，并制造一次越界访问观察 `DECERR`。

**操作步骤**：

1. 阅读 [modules/axi_lite/test/tb_axi_lite_mux.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/test/tb_axi_lite_mux.vhd) 与 [modules/axi_lite/test/tb_axi_lite_simple_crossbar.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/test/tb_axi_lite_simple_crossbar.vhd)，理解它们如何用 BFM 搭建多 slave 场景。
2. 参考 `module_axi_lite.py` 里 `axi_lite_mux` 的 netlist 构建（`base_addresses` 在 wrapper 里给出）。
3. 本地运行（待本地验证）：

   ```bash
   python tools/simulate.py axi_lite.tb_axi_lite_mux axi_lite.tb_axi_lite_simple_crossbar
   ```

**需要观察的现象**：访问落在 slave A 的地址 → 数据从 A 返回；落在 slave B 的地址 → 数据从 B 返回；落在两者之外的地址 → 响应 `resp = DECERR`（`"11"`），但 master 侧不挂死，能继续发后续事务。

**预期结果**：两组测试通过；`DECERR` 路径被覆盖。

#### 4.3.5 小练习与答案

**练习 1**：mux 的状态机为什么「只看 R 完成就回到 `wait_for_input`」，而不去检查 AR 是否也完成？

**参考答案**：依 AXI 标准 A3.3.1，slave 必须先完成 AR 握手才能拉 RVALID；所以 R 通道出现有效响应并完成握手，必然意味着 AR 已经在此之前完成。再看 AR 是冗余的。源码注释还提到，强行同时看 AR/R 虽然可能省一拍，但会增大控制信号扇出，伤害时序，所以本项目选择当前写法。

**练习 2**：`axi_lite_simple_write_crossbar` 里为什么要把每个输入的 `w.last <= '1'`？

**参考答案**：因为它复用的 AXI 写交叉栏依赖 `WLAST` 来判断写数据突发是否结束，而 AXI-Lite 恒为单拍写，所以把 `last` 硬置 1，让底层 AXI 仲裁逻辑正确地把每笔 Lite 写当作「一拍即结束」的突发来处理。

---

### 4.4 axi_lite_cdc 与 axi_lite_pipeline：跨时钟域与流水线

#### 4.4.1 概念说明

把 mux、crossbar、协议转换器拼成子系统时，常需要两件「胶水」：

- **跨时钟域（CDC）**：master 与 slave 跑在不同时钟上，必须安全跨域。
- **流水线**：组合路径太长、时序不收敛时，需要在握手通路上插寄存器。

`axi_lite_cdc` 与 `axi_lite_pipeline` 的设计手法与 u5-l3 讲过的 AXI CDC 如出一辙，也与 `axi_lite_cdc` 文件头注释的指引一致：**按通道拆**。AXI-Lite 有 AW/W/B/AR/R 五条通道，CDC 给每条通道各挂一个异步 FIFO，pipeline 给每条通道各挂一个 `handshake_pipeline`。两个实体本身**不写任何握手或同步逻辑**，只做「记录 ↔ 向量 ↔ FIFO/流水线 ↔ 向量 ↔ 记录」的接线。

这种「按通道拆 + 复用底层积木」的好处是：CDC 能力完全来自第 4 单元的 `asynchronous_fifo`（进而来自 `resync_counter`），时序收敛能力完全来自第 2 单元的 `handshake_pipeline`。Lite 子系统不重复造轮子。

#### 4.4.2 核心流程

CDC 把请求通道（AW/W/AR，master→slave）与响应通道（B/R，slave→master）按方向分别接到异步 FIFO 的写读侧：

```
master_clk 域                          slave_clk 域
  AW/W/AR ──打包(去valid)──> 异步FIFO ──> AW/W/AR
                                      (clk_write=master, clk_read=slave)

  B/R     <──解包──── 异步FIFO <──打包(去valid)── B/R
            (clk_read=master, clk_write=slave)
```

每条通道的 FIFO 宽度由 `axi_lite_*_sz` 函数精确算出，**不含 `valid`/`ready`**（控制位由 FIFO 的 `read_valid`/`write_ready` 重新产生）。流水线实体的结构与之同构，只是把 FIFO 换成 `handshake_pipeline`。

#### 4.4.3 源码精读

CDC 文件头明确要求套用 `asynchronous_fifo` 的约束（与 u5-l3 一致——Lite 自己没有约束文件）：

[modules/axi_lite/src/axi_lite_cdc.vhd:9-16](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_cdc.vhd#L9-L16) 注释：按通道用异步 FIFO 跨域，并通过宽度 generic 把总线打包到最优宽度；要求套用 `asynchronous_fifo` 的约束。

W 通道块：把记录打包成最窄向量交给 `asynchronous_fifo`，读出后再解包；`valid` 不进 FIFO，由 FIFO 的 `read_valid` 直接驱动：

[modules/axi_lite/src/axi_lite_cdc.vhd:86-118](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_cdc.vhd#L86-L118) `w_block`：打包 → `asynchronous_fifo`（`clk_write=clk_master`、`clk_read=clk_slave`）→ 解包。

B 通道是响应方向，时钟与数据流向反过来（`clk_write=clk_slave`、`clk_read=clk_master`），且 B 通道只搬 `resp` 这 2 位（`valid` 由 FIFO 产生）：

[modules/axi_lite/src/axi_lite_cdc.vhd:122-138](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_cdc.vhd#L122-L138) `b_asynchronous_fifo_inst`：方向反转，仅搬运 2 位 `resp`。

流水线实体把同样的五块结构换成 `handshake_pipeline`，并把 `full_throughput`/`pipeline_control_signals` 暴露为 generic（默认即全吞吐 skid buffer，可在面积与吞吐间取舍，承接 u2-l1）：

[modules/axi_lite/src/axi_lite_pipeline.vhd:9-16](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_pipeline.vhd#L9-L16) 注释：默认全 skid buffer（数据+控制都流水），改 generic 可降面积。

[modules/axi_lite/src/axi_lite_pipeline.vhd:34-46](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/src/axi_lite_pipeline.vhd#L34-L46) 暴露两个 `handshake_pipeline` generic，默认全吞吐、最低逻辑深度。

资源数字（来自 `module_axi_lite.py` 的 netlist 回归，器件 `xc7z020clg400-1`）量化了这些胶水的代价：

- `axi_lite_cdc`（`data_width=32, addr_width=24`）：199 LUT、290 FF、最大逻辑级数 4。
- `axi_lite_mux`：521 LUT、23 FF、最大逻辑级数 5。
- `axi_lite_simple_read_crossbar`（4 输入）：78 LUT、5 FF、最大逻辑级数 3。
- `axi_lite_simple_write_crossbar`（4 输入）：153 LUT、4 FF、最大逻辑级数 4。

#### 4.4.4 代码实践

**实践目标**：观察 CDC 在两时钟域不同快慢组合下都能正确传递 Lite 事务。

**操作步骤**：

1. 打开 [modules/axi_lite/test/tb_axi_lite_cdc.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/axi_lite/test/tb_axi_lite_cdc.vhd)。
2. 看 `module_axi_lite.py` 的 `setup_vunit` 如何为它登记三种时钟配置：`master_clk_fast`、`slave_clk_fast`、`same_clocks`。
3. 本地运行（待本地验证）：

   ```bash
   python tools/simulate.py axi_lite.tb_axi_lite_cdc
   ```

**需要观察的现象**：主快从慢、主慢从快、同频三种情形下，读写数据都正确往返；没有任何数据丢失或死锁。

**预期结果**：三组配置全部通过，证明 CDC 的方向性安全（来自底层 `asynchronous_fifo`）在 Lite 子系统里同样成立。

#### 4.4.5 小练习与答案

**练习 1**：`axi_lite_cdc` 为什么不自己写同步链，而是实例化 `asynchronous_fifo`？

**参考答案**：跨域的安全同步（格雷码指针、`async_reg` 同步链、`set_bus_skew`/`set_max_delay` 约束）已经由 `asynchronous_fifo` → `resync_counter` 完整解决（见 u4-l2、u3-l1）。CDC 只负责按通道接线，复用成熟积木既安全又省事；约束文件也直接套用 `asynchronous_fifo.tcl`。这正是「按通道拆 + 复用」的好处。

**练习 2**：B 通道的 FIFO 宽度只有 2 位（`axi_lite_s2m_b_sz = axi_resp_sz`），为什么？

**参考答案**：B 通道从到主的有效载荷只有 2 位的 `resp`；`valid` 是控制位，不进 FIFO（由 FIFO 的 `read_valid` 重新产生）。所以 B 通道 FIFO 只需搬 2 位，这是「打包时排除控制位、只搬实际数据」原则的直接体现，极大节省了资源。

---

## 5. 综合实践

把本讲的积木串成一条完整的寄存器访问链路：

```
AXI master(BFM) ──axi_to_axi_lite──> axi_lite_pipeline ──> axi_lite_mux ──> 多个 AXI-Lite slave(BFM)
                                          (可选)               (1-to-N 地址分发)
```

**任务**：

1. 参考 `tb_axi_to_axi_lite.vhd` 的实例化风格，搭一个最小 testbench：用 `bfm.axi_master` 做 master，用 `axi_to_axi_lite` 降级，再接一个 `bfm.axi_lite_slave` 做 slave。
2. 先完成一次写、一次读，校验地址/数据正确转换（这正是本讲规格里要求的实践任务）。
3. 在 `axi_to_axi_lite` 与 slave 之间**插入一个 `axi_lite_pipeline`**，重复读写，验证插了流水线后功能不变（仅多一拍延迟）。
4. （进阶）把单个 slave 换成 `axi_lite_mux` + 两个 slave，给两个不同的基址区间各写各读，再故意访问一个区间外的地址，确认返回 `DECERR` 且 master 不挂死。

**验收标准**：

- 步骤 2：`data_width=32` 下写后读数据一致。
- 步骤 3：插入流水线后功能不变，可在波形上看到握手多了一级寄存器。
- 步骤 4：两个 slave 各自响应正确；越界访问返回 `resp = DECERR`。

> 说明：本综合实践以**源码阅读 + 仿真搭建**为主。若本地已装好 tsfpga/VUnit/Vivado，可用 `python tools/simulate.py axi_lite.<tb名>` 跑现成测试台；否则按上述步骤自行搭建最小 testbench。命令的实际运行结果为「待本地验证」。

## 6. 本讲小结

- **AXI-Lite 是 AXI4 的单拍、32/64 位寄存器版**：没有突发、size、ID 等信号，最适合 CPU 配寄存器；握手规则与 AXI 一致。
- **`axi_lite_pkg` 用 record 分层聚合**五条通道，字段按最大位宽声明、打包时只取实际位并排除 `valid`/`prot` 等控制/罕用信号，体现「面积优先」。
- **`axi_to_axi_lite` 把完整 AXI 降级为 Lite**：只放行突发长度 1、整字大小的事务，非法者返回 `SLVERR`；并主动节流到「读写各自单事务在途」以兼容 Xilinx 硬核。
- **`axi_lite_mux`（1-to-N）**靠地址掩码解码 + 两态锁定机路由，无匹配地址时用虚拟 `DECERR` 端口兜底而不挂死；**`axi_lite_simple_read/write_crossbar`（N-to-1）**是复用 AXI 交叉栏的薄壳，硬塞 `last='1'` 适配单拍。
- **`axi_lite_cdc` 与 `axi_lite_pipeline`** 按通道各挂异步 FIFO / `handshake_pipeline`，本身不写握手与同步逻辑，能力全部复用自 `fifo`/`common` 模块。
- Lite 子系统的资源代价已被 netlist 回归量化（如 `axi_lite_cdc` 约 199 LUT/290 FF，`axi_lite_mux` 约 521 LUT）。

## 7. 下一步学习建议

- 下一讲 **u6-l1（通用寄存器文件核心）**将把本讲的 AXI-Lite 总线接到真正的寄存器阵列：精读 `register_file_pkg` 的寄存器模式（r/w/r_w 等）与 `axi_lite_register_file` 实体，届时你会看到 Lite 总线的「另一端」长什么样。
- 想巩固「按通道拆 FIFO」的手法，可回头对比 **u5-l3（AXI 跨时钟域与通道 FIFO）**，两套 CDC 同构。
- 想深入理解 `handshake_pipeline` 的 6 种模式如何影响流水线面积/吞吐，可重读 **u2-l1**，并尝试改 `axi_lite_pipeline` 的两个 generic 做 netlist 对比。
- 若对地址解码的掩码算法感兴趣，可阅读 `modules/common/src/addr_pkg.vhd` 的 `calculate_mask`/`decode`/`match` 实现，理解 mux 如何高效判定地址归属。

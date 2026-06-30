# axi_lite_regs：寄存器映射

## 1. 本讲目标

学完本讲，你应当能够：

- 用 `axi_lite_regs` 把一片（若干字节）内部寄存器同时暴露成 **AXI4-Lite 从端** 和 **硬件直连线**，并理解这两条访问路径如何共存。
- 配置 **只读字节**（`AxiReadOnly`）与 **特权/安全保护**（`PrivProtOnly` / `SecuProtOnly`），并说清它们各自如何影响 B/R 响应码。
- 说清 `wr_active_o` / `rd_active_o` / `reg_load_i` / `reg_q_o` 这一组硬件侧端口的用途，以及为什么 AXI 写和硬件直装载入同一字节时需要停顿。
- 读懂 `test/tb_axi_lite_regs.sv` 的「定向 + 随机」自检结构，并能仿照它写出自己的最小验证。

本讲承接 u12-l1（AXI-Lite 接口、`AXI_LITE_TYPEDEF_*` / `AXI_LITE_ASSIGN*` 宏、`axi_lite_join`）。我们会复用那里的接口外壳范式，但不再自己拼 RTL，而是剖析一个现成的、库内最常用的从端模块。

## 2. 前置知识

阅读本讲前，请确认你已了解：

- **AXI4-Lite 是 AXI4 的严格子集**：每笔事务恒为单拍（无 `len/size/burst/last`），无事务 ID、无原子操作，只保留 `addr/prot/data/strb/resp`（见 u12-l1）。
- **`prot[1:0]` 的含义**（AXI4 规范 A4.7）：`prot[0]` = 特权（privileged），`prot[1]` = 安全（secure），`prot[2]` = 指令/数据。本模块只用到低两位。
- **valid/ready 握手与 `spill_register`**：一次握手须 `valid && ready` 同高；`spill_register`（来自 `common_cells`）能在切断组合路径的同时给响应加一拍延迟（见 u4-l1、u7-l1）。
- **响应码**：`RESP_OKAY = 2'b00`、`RESP_SLVERR = 2'b10`（见 [src/axi_pkg.sv:91](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L91) 与 [src/axi_pkg.sv:97](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L97)）；`prot_t` 是 3 位类型 [src/axi_pkg.sv:54](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv#L54)。
- **字节选通 `wstrb`**：写事务中每个字节对应一位，`strb[i]=1` 表示要写入该字节。

一个直觉：`axi_lite_regs` 就像一个「双面寄存器堆」——一面朝着 AXI 总线（软件可见），一面朝着你的 RTL 逻辑（硬件可见）。两面都能读，写权限则被精心切分，这正是外设寄存器接口（状态寄存器、控制寄存器、只读常数表）的标准做法。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/axi_lite_regs.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv) | 本讲主角。定义结构体内核 `axi_lite_regs`（接受 `req_lite_t` / `resp_lite_t`）与接口外壳 `axi_lite_regs_intf`（端口是 `AXI_LITE.Slave`）。 |
| [test/tb_axi_lite_regs.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv) | 配套测试台。用随机 Lite 主端 + 定向读写 + 随机硬件直装载入 + 并发 checker 自检，是本库「定向随机验证」的典型范例。 |
| [src/axi_pkg.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_pkg.sv) | 提供 `RESP_OKAY` / `RESP_SLVERR`、`prot_t` 等常量与类型（Level 0 根包）。 |
| [src/axi_intf.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_intf.sv) | 提供 `AXI_LITE` / `AXI_LITE_DV` 接口（u12-l1 已讲）。 |

依赖关系：本讲只依赖 u12-l1。模块本身位于 Bender Level 2（依赖 `addr_decode`、`spill_register` 等 common_cells 原语）。

## 4. 核心概念与源码讲解

### 4.1 整体架构：一片字节寄存器，两条访问路径

#### 4.1.1 概念说明

`axi_lite_regs` 的本质是一组用触发器（FF）实现的、按 **字节** 寻址的寄存器阵列 `reg_q[RegNumBytes]`。它的特别之处在于这组字节同时挂在两个「面」上：

- **AXI4-Lite 面**（软件面）：通过 `axi_req_i` / `axi_resp_o` 被总线读写。
- **硬件面**（逻辑面）：通过 `reg_d_i` / `reg_load_i` / `reg_q_o` / `wr_active_o` / `rd_active_o` 直接接到周边 RTL。

这两个面不是互斥的，而是 **受控共存**：软件可写哪些字节、硬件可写哪些字节，由参数和运行期信号精细划分。模块头注释把这两个面讲得很清楚 [src/axi_lite_regs.sv:18-24](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L18-L24)。

一个关键设计：存储单元是 **字节粒度** 的，不是「32 位寄存器」粒度。`RegNumBytes` 直接数字节，`AxiReadOnly` 也是每字节一比特。这使得「一个 32 位寄存器里某些字节只读、某些可写」这样的配置天然支持。

#### 4.1.2 核心流程

模块可看作两条相对独立的通路加一组共享寄存器：

```text
               ┌─────────────── reg_q[RegNumBytes]（共享 FF 阵列）──────────────┐
               │                                                              │
   写通路:      │   AW+W → addr_decode 找 chunk → prot 校验 → RO/load 检查      │
               │            → 写 reg_d → FFLARN 入 reg_q                       │
               │                                                                  │
   读通路:      │   AR → addr_decode 找 chunk → prot 校验 → mux 读 reg_q_o      │
               │            → spill_register 给 R                               │
               │                                                                  │
   硬件面:      │   reg_load_i + reg_d_i → 直接写 reg_d → FFLARN 入 reg_q        │
               │   reg_q_o 永远把 reg_q 暴露给硬件                              │
               └──────────────────────────────────────────────────────────────────┘
```

写通路在握手当拍就把数据送入 `reg_d`，并在时钟沿由 `FFLARN` 写入 `reg_q`；读通路是纯组合地把 `reg_q_o` 拼到 R 通道，再经 `spill_register` 加一拍。硬件直装载入走同一条 `reg_d` 合并总线，但受停顿机制保护以免与 AXI 写冲突。

#### 4.1.3 源码精读

模块的参数与端口声明集中在一处 [src/axi_lite_regs.sv:60-130](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L60-L130)。几个最关键的参数：

- `RegNumBytes`：寄存器阵列的总字节数（不是「寄存器个数」），见 [src/axi_lite_regs.sv:62](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L62)。
- `PrivProtOnly` / `SecuProtOnly`：两比特独立的开关，要求来访事务的 `prot[0]` / `prot[1]` 必须为 1，见 [src/axi_lite_regs.sv:74](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L74) 与 [src/axi_lite_regs.sv:80](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L80)。
- `AxiReadOnly`：每字节一比特，`1` 表示该字节 **只能从 AXI 读、不能从 AXI 写**（但硬件面仍可写），见 [src/axi_lite_regs.sv:86](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L86)。
- `RegRstVal`：每字节一个复位值，支持上电即装载常数，见 [src/axi_lite_regs.sv:93](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L93)。

硬件侧端口（这一组是 `axi_lite_regs` 区别于普通 AXI 从端的核心）：

- `reg_q_o`：把每个字节的当前值持续暴露给周边逻辑 [src/axi_lite_regs.sv:129](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L129)。
- `reg_d_i` / `reg_load_i`：周边逻辑想把某字节改成新值时，把新值放 `reg_d_i[i]`、把load 允许位拉高 `reg_load_i[i]`，下一个时钟沿该字节即被载入 [src/axi_lite_regs.sv:116](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L116) 与 [src/axi_lite_regs.sv:127](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L127)。
- `wr_active_o` / `rd_active_o`：本拍有哪些字节正在被 AXI 写/读，是「事件脉冲」，供周边逻辑做副作用（如写 1 清零的状态位）[src/axi_lite_regs.sv:110](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L110) 与 [src/axi_lite_regs.sv:112](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L112)。

寄存器阵列本体由一行 `FFLARN` 宏（带加载使能与复位值的触发器，来自 `common_cells`）实例化，复位值逐字节取自 `RegRstVal` [src/axi_lite_regs.sv:315-318](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L315-L318)：

```systemverilog
for (genvar i = 0; i < RegNumBytes; i++) begin : gen_rw_regs
  `FFLARN(reg_q[i], reg_d[i], reg_update[i], RegRstVal[i], clk_i, rst_ni)
  assign reg_q_o[i] = reg_q[i];
end
```

注意 `reg_update[i]` 是统一的「该字节本拍是否要更新」信号——无论是 AXI 写还是硬件直装载入，最终都汇拢到它。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认「双面访问」的接线意图。

**步骤**：
1. 打开 [src/axi_lite_regs.sv:98-130](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L98-L130)，把端口分成三组：时钟复位、AXI 面（`axi_req_i`/`axi_resp_o`）、硬件面（其余 5 个）。
2. 在 [src/axi_lite_regs.sv:315-318](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L315-L318) 确认：每个字节的存储单元只有一个 FF，但 `reg_d[i]`（数据）和 `reg_update[i]`（使能）由两个面共同驱动。

**预期现象**：你会发现 AXI 面和硬件面 **不是** 各写各的独立寄存器，而是共同写同一组 `reg_q`；冲突由后续 4.3 的停顿逻辑解决。

**运行结果**：待本地验证（本任务为静态阅读，无需仿真）。

#### 4.1.5 小练习与答案

**练习 1**：为什么存储粒度选「字节」而不是「32 位字」？
**答案**：因为 AXI 写有字节选通 `wstrb`，按字节存储才能精确表达「一个字里只写某些字节」；同时也让 `AxiReadOnly` 能做到字节级只读，满足「状态寄存器中某些位只读、某些位可写」的真实需求。

**练习 2**：`reg_q_o` 是寄存器输出还是组合输出？
**答案**：寄存器输出。`reg_q` 本身是 FF，`assign reg_q_o[i] = reg_q[i]` 只是连线，所以 `reg_q_o` 反映的是 **上一个时钟沿** 写入的值。

---

### 4.2 地址映射：chunk 切分与 addr_decode

#### 4.2.1 概念说明

AXI4-Lite 的数据总线宽度通常是 32 位（即一次访问 4 字节），但 `RegNumBytes` 可以是任意字节数（测试台里就是 200）。于是模块把连续的 `AxiStrbWidth`（= `AxiDataWidth/8`）个字节打包成一个 **chunk**，每个 chunk 对应 AXI 总线的一次访问。地址译码以 chunk 为单位：给定一个地址，先找到它落在哪个 chunk，再去读写那一组字节。

这里复用了 u6-l2 里见过的 `addr_decode` 原语与 `rule_t` 规则结构，但规则表 **不是用户从外部喂入**，而是模块根据 chunk 数自动生成的。

#### 4.2.2 核心流程

设 `AxiDataWidth = 32`，则 `AxiStrbWidth = 4`。基本量：

\[
\text{AxiStrbWidth} = \text{AxiDataWidth}/8,\qquad
\text{NumChunks} = \lceil \text{RegNumBytes} / \text{AxiStrbWidth} \rceil
\]

每个 chunk `i` 覆盖字节区间 `[i*AxiStrbWidth,\ (i+1)*AxiStrbWidth)`，对应地址空间里一段前闭后开的规则。地址译码只看地址的最低若干位（`AddrWidth` 位），高位被忽略——这意味着模块的基址必须由上层对齐，模块自己只关心局部偏移。

对于「8 个 32 位寄存器」的常见配置（`RegNumBytes=32`、`AxiDataWidth=32`）：`AxiStrbWidth=4`、`NumChunks=8`，每条规则覆盖 4 字节，共 8 条规则，正好 8 个寄存器。

#### 4.2.3 源码精读

chunk 几何由三个 localparam 定义 [src/axi_lite_regs.sv:143-147](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L143-L147)：

```systemverilog
localparam int unsigned AxiStrbWidth  = AxiDataWidth / 32'd8;
localparam int unsigned NumChunks     = cf_math_pkg::ceil_div(RegNumBytes, AxiStrbWidth);
localparam int unsigned ChunkIdxWidth = (NumChunks > 32'd1) ? $clog2(NumChunks) : 32'd1;
```

地址规则表在生成块里逐 chunk 构建，注意区间是 **前闭后开**（`end_addr` 不含）[src/axi_lite_regs.sv:161-167](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L161-L167)：

```systemverilog
assign addr_map[i] = axi_rule_t'{
  idx:        i,
  start_addr: addr_t'( i   * AxiStrbWidth),
  end_addr:   addr_t'((i+1)* AxiStrbWidth)
};
```

模块用 **两份独立的 `addr_decode`** 分别译码 AW 与 AR（写、读各自一份），输出 `aw_chunk_idx` / `ar_chunk_idx` 与 `aw_dec_valid` / `ar_dec_valid` [src/axi_lite_regs.sv:320-348](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L320-L348)。地址在送入译码器前被截断为 `AddrWidth` 位 [src/axi_lite_regs.sv:326](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L326)，即「只看低位」。

关于 `AddrWidth` 的取值 [src/axi_lite_regs.sv:151](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L151)：当 `RegNumBytes>1` 时为 `$clog2(RegNumBytes)+1`。多出的高位宽度让译码器能区分「落在范围内」与「落到最后一个 chunk 之后的越界地址」——后者匹配不到任何规则，`dec_valid` 为 0，从而在写/读通路被判为 `SLVERR`（见模块头注释 [src/axi_lite_regs.sv:30-34](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L30-L34)）。

#### 4.2.4 代码实践（参数推演型）

**目标**：手工推演「8 个 32 位寄存器」配置下的地址映射。

**步骤**：
1. 取 `RegNumBytes=32`、`AxiDataWidth=32`，代入 [src/axi_lite_regs.sv:143-145](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L143-L145) 算出 `AxiStrbWidth`、`NumChunks`、`ChunkIdxWidth`。
2. 列出 8 条 `addr_map` 规则的 `start_addr` / `end_addr`。
3. 回答：地址偏移 `0x06` 落在第几个 chunk？地址偏移 `0x20`（=32）呢？

**预期结果**：`AxiStrbWidth=4`、`NumChunks=8`、`ChunkIdxWidth=3`；规则为 `{0,4},{4,8},…,{28,32}`；偏移 `0x06` → chunk 1（字节 4–7）；偏移 `0x20` 超出末字节（31），`dec_valid=0` → 响应 `SLVERR`。

**运行结果**：待本地验证（可用任意 SV 仿真器把 `RegNumBytes=32` 编进 `tb_axi_lite_regs` 后观察）。

#### 4.2.5 小练习与答案

**练习 1**：若 `RegNumBytes=10`、`AxiDataWidth=32`，`NumChunks` 是多少？最后一个 chunk 是否「填不满」？
**答案**：`NumChunks = ceil(10/4) = 3`。最后一个 chunk 覆盖字节 `[8,12)`，但实际只有字节 8、9 存在；字节 10、11 落入该 chunk 但越界，读时由 [src/axi_lite_regs.sv:303-305](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L303-L305) 补 0。

**练习 2**：为什么 AW 和 AR 要各用一份 `addr_decode` 而不是共用？
**答案**：因为写和读是两条独立通路，各自的地址在时间上互不相关，且 `addr_decode` 是纯组合原语、不带状态；分用两份让两条通路的时序路径独立，便于切割与优化。

---

### 4.3 写通路：只读保护、prot 校验与 load 冲突停顿

#### 4.3.1 概念说明

写通路是本模块最精巧的部分，因为它要同时处理四种「写不写得进去」的判定：

1. **地址是否落在合法 chunk**（`aw_dec_valid`）。
2. **保护位是否达标**（`aw_prot_ok`：若开了 `PrivProtOnly`/`SecuProtOnly`，`prot` 相应位必须为 1）。
3. **该字节是否只读**（`AxiReadOnly[i]`）：只读字节 AXI 写不进去，但硬件面可以写。
4. **该字节是否正被硬件直装载入**（`reg_load_i[i]` 且非只读）：若是，AXI 写必须 **停顿**，等硬件载入完成，避免同一拍两路同时写。

只有 1、2 都通过，写才能进行；3 决定字节是否真正被改写以及 B 响应码；4 决定是否拉低 `aw_ready`/`w_ready` 把事务挂起。

#### 4.3.2 核心流程

写通路的判定树（对应 `always_comb` 块 [src/axi_lite_regs.sv:213-273](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L213-L273)）：

```text
当 aw_valid && w_valid && b_ready:
  ├─ 若 aw_dec_valid && aw_prot_ok:
  │    ├─ 若 chunk_loaded（该 chunk 有非只读字节正被硬件载入且被 strb 选中）:
  │    │     → 不动 aw_ready/w_ready  ⇒ 事务停顿（保住 valid 不撤的铁律）
  │    └─ 否则:
  │         ├─ 对 chunk 内每个字节 i:
  │         │    ├─ 非只读 && strb[i]:   reg_d[byte]=w.data, reg_update=1  (真正写入)
  │         │    └─ wr_active[byte] = strb[i]            (无论是否只读，都反映"被选中")
  │         └─ b_chan.resp = chunk_ro ? SLVERR : OKAY     (整 chunk 全只读才报错)
  │            aw_ready=w_ready=1
  └─ 否则（地址越界或 prot 不达标）:
       → b_chan.resp = SLVERR（默认值）, b_valid=1, aw_ready=w_ready=1   (吸收事务并报错)
```

两条要点：

- **整 chunk 全只读才返回 `SLVERR`**：只要本次写至少真正改写了一个非只读字节，就回 `OKAY`；只有 `wstrb` 选中的全是只读字节时才报错（见模块头注释 [src/axi_lite_regs.sv:41-43](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L41-L43)）。这等价于「写了至少一个字节就算成功」。
- **`wr_active_o` 与是否只读无关**：即便写打到只读字节上，对应的 `wr_active` 位也会拉高一拍，让周边逻辑能感知「软件试图写只读位」这一事件。

#### 4.3.3 源码精读

保护位校验用两个三元表达式把「不开保护」短路为 1 [src/axi_lite_regs.sv:187-188](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L187-L188)：

```systemverilog
assign aw_prot_ok = (PrivProtOnly ? axi_req_i.aw.prot[0] : 1'b1) &
                    (SecuProtOnly ? axi_req_i.aw.prot[1] : 1'b1);
```

load 冲突的判定分两步。先按 chunk 内的 4 个字节位置算出每个位置的 `load`（非只读且正被硬件载入）与 `read_only` 标志 [src/axi_lite_regs.sv:196-205](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L196-L205)；再把它们与 `w.strb` 结合 [src/axi_lite_regs.sv:208-209](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L208-L209)：

```systemverilog
assign chunk_loaded = |(load & axi_req_i.w.strb);   // 有非只读字节既被硬件载入又被 AXI 选中
assign chunk_ro     = &read_only;                   // 整个 chunk 是否全只读
```

写主逻辑里，真正改写字节的判据是「非只读 **且** 被选通」[src/axi_lite_regs.sv:254-258](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L254-L258)，而 `wr_active` 只看选通位：

```systemverilog
if (!AxiReadOnly[reg_byte_idx] && axi_req_i.w.strb[i]) begin
  reg_d[reg_byte_idx]      = axi_req_i.w.data[8*i+:8];
  reg_update[reg_byte_idx] = 1'b1;
end
wr_active_o[reg_byte_idx] = axi_req_i.w.strb[i];
```

B 响应码取决于 `chunk_ro` [src/axi_lite_regs.sv:261](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L261)：

```systemverilog
b_chan.resp = chunk_ro ? axi_pkg::RESP_SLVERR : axi_pkg::RESP_OKAY;
```

注意 `always_comb` 开头先把硬件直装载入并入 `reg_d` [src/axi_lite_regs.sv:229-234](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L229-L234)，随后 AXI 写对非只读字节会覆盖同一 `reg_d[i]`——但正如 4.3.2 所述，`chunk_loaded` 停顿保证了「同一非只读字节不会被两路同时选中」，因此不会发生真实的数据竞争；而只读字节 AXI 根本不写，所以只读字节的硬件直装载入可以和 AXI 写同拍进行（这正是注释 [src/axi_lite_regs.sv:252-253](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L252-L253) 所说的「允许对未写字节并行直载」）。

> 提示：`b_ready` 来自 B 通道的 `spill_register` 输出，是寄存器信号，因此可以安全地用作组合条件（见注释 [src/axi_lite_regs.sv:237](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L237)）。

#### 4.3.4 代码实践（参数配置 + 行为预测型）

**目标**：预测「8 个 32 位寄存器，第 0 个只读」配置下，几笔典型写事务的 B 响应。

**配置**：`RegNumBytes=32`，`AxiDataWidth=32`，`AxiReadOnly[3:0]=4'b1111`（第一个寄存器 4 字节全只读），其余位为 0。

**步骤**：对下面三笔写（均 `prot=0`、`strb=4'b1111`），用 4.3.2 的判定树给出 B 响应，并指出哪些字节被真正改写、哪些 `wr_active` 位会拉高：

1. 写偏移 `0x00`（reg 0），数据 `0xDEADBEEF`。
2. 写偏移 `0x04`（reg 1），数据 `0x12345678`。
3. 写偏移 `0x04`、`strb=4'b0000`（空写）。

**预期结果**：
1. chunk 0 全只读 → `chunk_ro=1` → B=`SLVERR`；4 个字节都不改写；`wr_active[3:0]=4'b1111`（事件仍报告）。
2. chunk 1 全可写 → `chunk_ro=0` → B=`OKAY`；字节 4–7 被写成 `0x12345678`；`wr_active[7:4]=4'b1111`。
3. `strb` 全 0，没有任何字节被选中；按 [src/axi_lite_regs.sv:261](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L261) 因 `chunk_ro=0` 仍回 `OKAY`（但无任何字节变化、`wr_active` 全 0）。这一边界行为待本地验证。

**运行结果**：待本地验证（可改 `tb_axi_lite_regs` 的 `TbAxiReadOnly` 后仿真观察）。

#### 4.3.5 小练习与答案

**练习 1**：如果想让某寄存器「软件只能读、硬件周期性更新」（典型的硬件状态寄存器），参数该怎么设？
**答案**：把该寄存器对应的 4 字节在 `AxiReadOnly` 里置 1；硬件侧持续驱动 `reg_d_i` 并在更新拍拉高 `reg_load_i`。只读字节不受 `chunk_loaded` 停顿约束，可照常直载。

**练习 2**：为什么 `chunk_loaded` 停顿只看「非只读」字节？
**答案**：因为只读字节 AXI 写不进去（[src/axi_lite_regs.sv:254](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L254) 的判据已排除只读），AXI 与硬件直载不会在只读字节上争用同一 `reg_d[i]`，自然无需停顿。

**练习 3**：`PrivProtOnly=1` 时，一笔 `prot[0]=0` 的写会怎样？
**答案**：`aw_prot_ok=0`，落到 [src/axi_lite_regs.sv:266-271](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L266-L271) 的 else 分支：吸收事务（`aw_ready=w_ready=1`）、回默认 `SLVERR`、不读不写任何寄存器。

---

### 4.4 读通路与 spill_register

#### 4.4.1 概念说明

读通路比写通路简单：AR 握手后，按 `ar_chunk_idx` 把对应 4 字节从 `reg_q_o` 组合地拼到 R 通道数据上。模块在 R（以及 B）通道上各插了一级 `spill_register`，作用有二：切断「slave 输入到输出」的组合路径（改善时序），并给响应加一拍延迟，使握手更规整。这两点与 u4-l1 讲的 `axi_cut` 同源。

读错误（地址越界或 prot 不达标）时，模块用一个固定的「错误数据」`0xBA5E1E55` 配 `RESP_SLVERR` 返回——注意这与 `axi_err_slv` 用的 `0xBADCAB1E`（u6-l2）是不同模块、不同魔数，不要混淆。

#### 4.4.2 核心流程

```text
AR 来访:
  ├─ ar_dec_valid && ar_prot_ok:
  │     对 chunk 内字节 i:  r_chan.data[8*i+:8] = reg_q_o[ar_chunk_idx*4 + i]
  │     越界字节:            r_chan.data[8*i+:8] = 8'h00
  │     r_chan.resp = OKAY
  └─ 否则（越界或 prot 不达标）:
        r_chan.data = 32'hBA5E1E55,  r_chan.resp = SLVERR
然后 r_chan 经 spill_register 送出 R；ar_ready 由 spill_register 的 ready_o 驱动。
```

#### 4.4.3 源码精读

保护校验与写通路对称 [src/axi_lite_regs.sv:281-282](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L281-L282)。读 mux 默认值就是错误响应 [src/axi_lite_regs.sv:287-291](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L287-L291)：

```systemverilog
r_chan = r_chan_lite_t'{
  data: axi_data_t'(32'hBA5E1E55),
  resp: axi_pkg::RESP_SLVERR,
  default: '0
};
```

合法读时按字节拼装，并在握手成立的拍拉高对应 `rd_active` [src/axi_lite_regs.sv:295-308](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L295-L308)。注意 `rd_active_o[reg_byte_idx] = r_valid & r_ready`，即只有在真正握手时才报事件。

R 与 B 通道各有一份 `spill_register`（`Bypass=0`，即总是启用）[src/axi_lite_regs.sv:351-378](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L351-L378)。`r_valid` 直接连到 `ar_valid`（送进 spill），`ar_ready` 由 spill 的 `ready_o` 驱动 [src/axi_lite_regs.sv:311-312](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L311-L312)。

#### 4.4.4 代码实践（测试台对照型）

**目标**：读懂测试台如何建模「期望读数据」并自检。

**步骤**：打开 [test/tb_axi_lite_regs.sv:161-214](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L161-L214) 的 `proc_check_read_data`。注意它：
1. 在 AR 握手拍，按同一套 chunk 公式从 `reg_q`（DUT 输出）算期望 `r_data`，越界字节填 `8'hxx`（不确定，跳过比对）。
2. 在 R 握手拍，逐字节比对；对 `SLVERR` 则断言数据恰为 `32'hBA5E1E55`（[test/tb_axi_lite_regs.sv:208](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L208)）。

**预期现象**：因为 checker 直接读 DUT 的 `reg_q` 当黄金模型，读写会自然一致；唯一「不确定」的是越界字节，用 `8'hxx` 容忍。

**运行结果**：待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么读通路不需要像写通路那样关心「硬件直载冲突」？
**答案**：读只是把 `reg_q_o` 组合地送出，不改写任何字节；硬件直载与读可以在同一拍进行，读到的要么是旧值要么是新值，都是合法的（AXI 不规定同拍读写谁先）。

**练习 2**：R 通道的 `spill_register` 把响应延后了一拍，这会让 AR 与 R 的握手关系变成什么样？
**答案**：`ar_ready` 不再是组合直接产生，而是来自 spill 的 `ready_o`；当 spill 里已有一个待取走的 R 而 master 未取走时，`ar_ready` 会被压低，形成对 AR 的反压。这是标准的「一刀切路径 + 一拍」代价。

---

### 4.5 接口外壳与硬件侧端口的典型用法

#### 4.5.1 概念说明

内核 `axi_lite_regs` 的 AXI 端口是 `req_lite_t` / `resp_lite_t` 结构体（u2-l4 的范式）。库同时提供接口外壳 `axi_lite_regs_intf`，把 AXI 端口换成 `AXI_LITE.Slave` 接口，并用大写参数名（`REG_NUM_BYTES` 等）匹配接口版世界的一贯风格。绝大多数用户（包括测试台）都例化 `_intf` 版本。

本节还要把 4.1 提到的硬件侧端口讲成「能拿来干什么」：它们让 `axi_lite_regs` 不只是一个被动的存储从端，而是能与周边逻辑双向互动的小内核。

#### 4.5.2 核心流程与典型用法

| 端口 | 方向 | 典型用法 |
|------|------|----------|
| `reg_q_o[i]` | 模块→逻辑 | 把寄存器当前值喂给 datapath（如使能位、配置字）。 |
| `reg_d_i[i]` / `reg_load_i[i]` | 逻辑→模块 | 硬件产生新值并载入（如计数器、状态机把状态写回）。只读字节也可这样载入。 |
| `wr_active_o[i]` | 模块→逻辑 | 「软件刚写了这一字节」事件脉冲，常用于「写 1 清零」的状态位：软件写 1 → 硬件见 `wr_active` → 下一拍清状态。 |
| `rd_active_o[i]` | 模块→逻辑 | 「软件刚读了这一字节」，可用于触发读取副作用（如读后自增的指针）。 |

#### 4.5.3 源码精读

接口外壳用 `AXI_LITE_TYPEDEF_*` 宏生成 5 个通道与 req/resp 类型，再用 `AXI_LITE_ASSIGN_TO_REQ` / `AXI_LITE_ASSIGN_FROM_RESP` 在 `AXI_LITE.Slave` 接口与结构体之间搬运，最后例化内核 [src/axi_lite_regs.sv:438-472](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L438-L472)。这正是 u2-l4 / u12-l1 反复强调的「接口外壳 + 结构体内核」范式：

```systemverilog
`AXI_LITE_ASSIGN_TO_REQ  (axi_lite_req,  slv)
`AXI_LITE_ASSIGN_FROM_RESP(slv, axi_lite_resp)

axi_lite_regs #(...) i_axi_lite_regs (
  .axi_req_i ( axi_lite_req  ),
  .axi_resp_o( axi_lite_resp ),
  // 硬件侧端口直接穿墙透传到顶层
  ...
);
```

测试台正是例化 `_intf` 版本 [test/tb_axi_lite_regs.sv:345-362](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L345-L362)，把 `master` 接口接成 `slv`，并把 `wr_active` / `rd_active` / `reg_d` / `reg_load` / `reg_q` 全部拉到 TB 顶层用于自检与激励。

模块还内建了一组仿真期断言（`pragma translate_off`），其中最有教益的是「只读字节在未被硬件直载时绝不能被 AXI 改动」[src/axi_lite_regs.sv:401-404](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L401-L404)：

```systemverilog
assert property (@(posedge clk_i) (!reg_load_i[i] && AxiReadOnly[i] |=> $stable(reg_q_o[i])))
    else $fatal(1, "Read-only register at `byte_index: %0d` was changed by AXI!", i);
```

这条断言把「只读」这一合同钉死：只要 `AxiReadOnly[i]=1` 且本拍没硬件直载，下一拍该字节必须稳定——任何 AXI 写都不允许改变它。

#### 4.5.4 代码实践（最小例化型）

**目标**：写一段最小例化，把 8 个 32 位寄存器挂到 `AXI_LITE.Slave` 上，并让硬件侧把 reg 0 配成只读常数。

**示例代码**（不是项目原有代码，仅作示范）：

```systemverilog
// 8 个 32 位寄存器 = 32 字节；reg0 (字节 0..3) 只读
localparam int unsigned N_BYTES = 32;
logic [N_BYTES-1:0] ro_mask = '0;
assign ro_mask[3:0] = 4'b1111;            // 第 0 个寄存器只读

axi_lite_regs_intf #(
  .REG_NUM_BYTES ( N_BYTES    ),
  .AXI_ADDR_WIDTH( 32'd32     ),
  .AXI_DATA_WIDTH( 32'd32     ),
  .AXI_READ_ONLY ( ro_mask    ),          // 也可直接写 parameter 字面量
  .REG_RST_VAL   ( '0         )           // reg0 想做常数表可在这里填初值
) i_regs (
  .clk_i, .rst_ni,
  .slv         ( my_lite_bus ),           // AXI_LITE.Slave
  .wr_active_o ( /* unused */ ),
  .rd_active_o ( /* unused */ ),
  .reg_d_i     ( '0          ),           // 不用硬件直载
  .reg_load_i  ( '0          ),
  .reg_q_o     ( reg_values  )            // 32 字节当前值，供 datapath 用
);
```

**步骤**：
1. 把 `ro_mask[3:0]` 设为只读，确认 reg0 不能被 AXI 写改。
2. 若想让 reg0 成为「只读常数表」，按模块头注释 [src/axi_lite_regs.sv:47-53](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L47-L53) 的三步：`AxiReadOnly` 置 1、`reg_load_i` 恒 0、`RegRstVal` 填想要的常数；综合后这些字节可能被优化成 LUT 常数而非 FF。

**预期结果**：reg0 读出为 `RegRstVal` 给定的常数，AXI 写 reg0 回 `SLVERR` 且值不变；reg1–reg7 可正常读写。

**运行结果**：待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：`wr_active_o` 与「字节是否被真正改写」是一回事吗？
**答案**：不是。`wr_active_o[i] = strb[i]`，只反映「软件选中了这一字节」，与该字节是否只读无关；只读字节被选中时 `wr_active` 也会拉高，但字节并不被改写。这正是它能用来「探测软件试图写只读位」的原因。

**练习 2**：为什么测试台要把 `reg_d` / `reg_load` 也驱动起来，而不是恒 0？
**答案**：为了压测 4.3 的 `chunk_loaded` 停顿路径与「只读字节可被硬件直载」的行为；若恒 0，这两条代码路径在仿真里根本走不到，覆盖度会缺失。

---

## 5. 综合实践

把本讲四块内容串起来：为一个外设配置一组「混合权限」寄存器并自检。

**场景**：一个外设有 8 个 32 位寄存器：

- **Reg0**：只读硬件状态字（硬件周期性更新，软件只读）。
- **Reg1**：可写控制字（软件读写）。
- **Reg2**：只读常数 ID = `0xCAFE0002`（用 `RegRstVal` + `AxiReadOnly` + `reg_load_i=0` 实现）。
- **Reg3–Reg7**：普通读写寄存器。
- 整个模块同时开 `PrivProtOnly=1`（只允许特权访问）。

**任务**：

1. 写出 `AxiReadOnly`（32 位）与 `RegRstVal`（32 字节）两个参数的具体值。
2. 用 `axi_lite_rand_master`（参考 [test/tb_axi_lite_regs.sv:54-65](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L54-L65)）发起：
   - 一笔非特权（`prot[0]=0`）写 Reg1 → 期望 `SLVERR`；
   - 一笔特权（`prot[0]=1`）写 Reg1 → 期望 `OKAY` 且读回一致；
   - 一笔特权写 Reg0 → 期望 `SLVERR`（只读），且读回仍是硬件更新的值；
   - 一笔特权读 Reg2 → 期望 `OKAY` 且数据为 `0xCAFE0002`。
3. 用一个独立 `initial` 块周期性地 `reg_load_i[3:0]=4'b1111; reg_d_i[3:0]=<新值>` 更新 Reg0，验证软件读到的 Reg0 会随硬件更新而变化（这正是「双面访问」的体现）。
4. 参照 [test/tb_axi_lite_regs.sv:288-300](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/test/tb_axi_lite_regs.sv#L288-L300) 的 `check_q` 任务，对只读字节断言「AXI 写不改变其值」。

**参考答案要点**：

- `AxiReadOnly = 32'b0000_0000_0000_0000_0000_0000_0000_1111`（字节 0–3 只读；Reg2 对应字节 8–11 不必设只读，因为常数表靠 `reg_load_i=0` 保证不被改写，但为保险也可把字节 8–11 也设只读）。
- `RegRstVal` 的字节 8–11 填 `0xCAFE0002`（小端：字节 8=`0x02`、9=`0x00`、10=`0xFE`、11=`0xCA`），其余按需。
- 非特权访问因 `aw_prot_ok=0` 走 [src/axi_lite_regs.sv:266-271](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_regs.sv#L266-L271) 的错误分支回 `SLVERR`。

**运行结果**：待本地验证（需要 VSIM/Verilator 等仿真器与 Bender 环境，见 u1-l4）。

## 6. 本讲小结

- `axi_lite_regs` 是一组 **字节粒度** 的 FF 阵列，同时挂在 AXI4-Lite 从端（`axi_req_i`/`axi_resp_o`）与硬件直连面（`reg_d_i`/`reg_load_i`/`reg_q_o`/`wr_active_o`/`rd_active_o`）上。
- 地址以 **chunk** 为单位译码（`AxiStrbWidth` 字节/chunk），用两份自动生成规则表的 `addr_decode` 分别服务 AW、AR；越界地址回 `SLVERR`。
- 写通路四重判定：地址合法、`prot` 达标、字节非只读、无硬件直载冲突；只要真正写了一个字节就回 `OKAY`，全命中只读才回 `SLVERR`；`wr_active_o` 只反映「被选中」与只读无关。
- 读通路组合地拼 `reg_q_o` 到 R，错误读返回固定数据 `0xBA5E1E55` + `SLVERR`；R/B 各经一级 `spill_register` 切组合路径并加一拍。
- `PrivProtOnly` / `SecuProtOnly` 用 `prot[0]` / `prot[1]` 做门禁，不达标的事务被吸收并回 `SLVERR`，不读不写。
- 库提供接口外壳 `axi_lite_regs_intf`（`AXI_LITE.Slave` 端口）；测试台 `tb_axi_lite_regs` 是「定向 + 随机 + 硬件直载 + 并发 checker」自检的范本。

## 7. 下一步学习建议

- **横向对比**：阅读 [src/axi_lite_mailbox.sv](https://github.com/pulp-platform/axi/blob/e55ae2a7ee606ee3cfd4257f63982a971b704407/src/axi_lite_mailbox.sv) 与 `tb_axi_lite_mailbox`（u12-l4），看双从端邮箱如何复用本讲的接口范式。
- **向上承接**：进入 u13-l1（`axi_to_axi_lite` / `axi_lite_to_axi`），那里会把完整 AXI4 突发拆成单拍喂给类似 `axi_lite_regs` 的 Lite 从端——你会更理解为何 Lite 从端只处理单拍。
- **协议深读**：若你对 `prot` 与原子操作感兴趣，可跳到 u15-l1（ATOPs），对照理解 `prot`、`atop` 在完整 AXI4/AXI5 里的角色。
- **动手方向**：基于本讲综合实践，尝试把 `axi_lite_regs` 接到 u12-l2 的 `axi_lite_xbar` 上，搭一个「多主 → xbar → 寄存器从端」的最小系统并跑随机回归。

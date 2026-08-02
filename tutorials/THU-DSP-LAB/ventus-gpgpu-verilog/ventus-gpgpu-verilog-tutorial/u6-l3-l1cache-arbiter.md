# L1 cache 仲裁 l1cache_arb

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `l1cache_arb` 在 SM（流处理器核）里的位置：它是 SM **内部**各 L1 缓存与 SM **对外**存储接口之间的「复用器/解复用器」。
- 解释为什么 icache、dcache 需要被仲裁复用，而 **shared_memory 不参与仲裁**（这是本讲最容易被误解、也是最重要的一个事实纠正）。
- 读懂 `l1cache_arb.v` 的固定优先级仲裁 + 二进制选择 + source 标签 + 响应路由的完整组合逻辑。
- 把 `cache_invalid` 信号从 GPGPU_top 的 `wg_done` 一路追到 icache 的整表无效、dcache 的失效请求，最终回到 `host_rsp_valid_o` 的整条冲刷（flush）握手链。

本讲承接 u5-l1（访存单元 LSU）。在 u5-l1 里我们看到 LSU 用 `addr < SHAREMEM_SIZE` 把请求分流到共享内存或 D-cache；本讲回答分流之后、离开 SM 之前的「最后一公里」：D-cache 与 I-cache 的请求如何合并成一路对外。

## 2. 前置知识

- **TileLink 风格接口（A/D 通道）**：本项目里 L1↔L2 之间用类 TileLink 的双通道握手。A 通道（`mem_req_*`）发请求，D 通道（`mem_rsp_*`）回响应。`valid/ready` 同时为高才算一次有效握手。不熟悉的话先记住「请求走 A，响应走 D」即可，详细操作码见 u7-l1。
- **仲裁器（arbiter）**：当多个部件争用同一个共享资源（这里是对外的单一 A 通道）时，需要一个仲裁器决定「这一拍让谁先走」。固定优先级仲裁器总是让编号最小的请求者先走。
- **one-hot 与 binary**：`one-hot` 指只有一位为 1（如 `2'b01`、`2'b10`），用来「点名」选中某一路；`binary` 是普通二进制编号（如 `1'd0`、`1'd1`），用来在多路总线里做数据选择（MUX）的下标。
- **cache invalid / flush**：workgroup 跑完后，主机要读回结果。为了确保 D-cache 里缓存的脏数据已经写回下层、且 I-cache 里旧指令不会干扰下一个 workgroup，需要把缓存「冲刷」一遍。`cache_invalid` 就是这个冲刷的触发信号。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/gpgpu_top/sm/l1cache_arb.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache_arb.v) | **本讲主角**。把 NUM_CACHE_IN_SM 路 L1 请求仲裁复用成一路对外 A 通道，并把对外的 D 通道响应按 source 路由回正确的缓存。纯组合逻辑。 |
| [src/gpgpu_top/sm/sm_wrapper.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v) | SM 顶层。例化 `l1cache_arb`、`instruction_cache`、`l1_dcache`、`shared_mem`，是看清「谁进了仲裁器、谁没进」的连线图。还包含 `cache_invalid` 在 SM 内的处理逻辑。 |
| [src/gpgpu_top/GPGPU_top.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v) | 芯片顶层。产生 `cache_invalid`（由 `wg_done` 驱动），并等待 `l2cache_finish_issue` 后才回报主机 `host_rsp_valid_o`。 |
| [src/define/define.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v) | 定义 `NUM_CACHE_IN_SM`、`NUM_CACHE_DEPTH`、`A_SOURCE`、`D_SOURCE` 等决定仲裁器规模与 source 位宽的宏。 |
| [src/common_cell/fixed_pri_arb.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fixed_pri_arb.v) | 仲裁器复用的公共单元：固定优先级，选最低位。 |
| [src/common_cell/one2bin.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/one2bin.v) | 公共单元：one-hot 转 binary，用于把仲裁结果变成 MUX 选择下标。 |
| [src/gpgpu_top/sm/l1cache/dcache/dcache_control.v](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/dcache_control.v) | D-cache 控制状态机。用来确认 `opcode==3'b011` 的几种 param（invalidate/flush/wait_mshr）语义。 |

## 4. 核心概念与源码讲解

### 4.1 SM 对外存储接口与「到底谁需要被仲裁」

#### 4.1.1 概念说明

一个 SM 内部有三个会访问存储的部件：

1. **icache（指令缓存）**：取指阶段按 PC 发请求，缺失时向 L2 取指令块。
2. **dcache（数据缓存）**：LSU 的 load/store 请求，缺失时向 L2 取数据块，脏替换时向 L2 写回。
3. **shared_memory（共享内存 / LDS）**：同 workgroup 内线程共享的低延迟暂存区（u6-l2）。

前两者（icache、dcache）命中不了的请求都需要**离开 SM、去片上互联找 L2**。而 SM 对外只有**一组** TileLink 风格接口（一对 A/D 通道，见 `sm_wrapper.v` 的 `mem_req_*` / `mem_rsp_*` 端口）。两组请求抢一个出口，就需要一个仲裁器把它们**复用（multiplex）**成一路——这就是 `l1cache_arb` 存在的原因。

shared_memory 则完全不同：它**命中即返回、永不缺失、不离开 SM**（u6-l2 已讲），所以它**不参与对 L2 的访问，也不经过 `l1cache_arb`**。这一点非常关键，也是本讲义主题描述里「icache/dcache/shared 三者一起被仲裁」的**纠正**：实际上只有 icache 和 dcache 两路进仲裁器。

规模由宏 `NUM_CACHE_IN_SM` 决定：

[src/define/define.v:L39-L39](https://github.com/THU-DSP-LAB-ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L39-L39) —— 注释明确写着 "the number of l1cache in a sm"，取值为 **2**。所以 `l1cache_arb` 是一个 2 选 1 的复用器。

```verilog
`define NUM_CACHE_IN_SM 2 //the number of l1cache in a sm
```

#### 4.1.2 核心流程

SM 对外存储通路可以抽象成下面的结构（注意 shared_mem 的位置）：

```
                 ┌──────────┐
   icache ──────▶│          │       A 通道
   (取指缺失)    │          │──────mem_req_*────────▶ 对外 (l2cache_arb→L2)
                 │ l1cache  │
   dcache ──────▶│  _arb    │◀──────mem_rsp_*──────── 对外 (L2 响应)
   (load/store) │          │       D 通道
                 └──────────┘

   shared_mem ──（直接挂在 pipe 上，自闭环，不出 SM，不经 l1cache_arb）
```

请求方向（A 通道）：`icache`、`dcache` 两路 → `l1cache_arb` → 单路对外。
响应方向（D 通道）：单路对外 → `l1cache_arb` → 拆回两路分别给 `icache`、`dcache`。

#### 4.1.3 源码精读

先看 `sm_wrapper` 怎么把 icache 和 dcache 接进仲裁器。注意拼接顺序——大端在左，所以 `dcache` 在高位（index 1）、`icache` 在低位（index 0）：

[src/gpgpu_top/sm/sm_wrapper.v:L430-L462](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L430-L462) —— 例化 `l1cache_arb`，每个数组端口都拼成 `{dcache_*, icache_*}`，共两路。

```verilog
l1cache_arb l1cache_arb(
  .mem_req_in_ready_o    ({dcache_mem_req_ready,icache_mem_req_ready}      ),
  .mem_req_in_valid_i    ({dcache_mem_req_valid,icache_mem_req_valid}      ),
  ...
  .mem_req_out_ready_i   (mem_req_ready_i   ),  // 对外 A 通道（单路）
  .mem_req_out_valid_o   (mem_req_valid_o   ),
  ...
  .mem_rsp_out_ready_i   ({dcache_mem_rsp_ready,icache_mem_rsp_ready}      ),  // 对外 D 通道拆两路
  .mem_rsp_out_valid_o   ({dcache_mem_rsp_valid,icache_mem_rsp_valid}      ),
  ...
```

对比 `shared_mem` 的例化——它的端口直接连到 `pipe_*` 信号，与 `l1cache_arb` **没有任何连接**：

[src/gpgpu_top/sm/sm_wrapper.v:L498-L517](https://github.com/THU-DSP-LAB-ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L498-L517) —— `shared_mem` 直接接 `pipe_shared_req_*` / `pipe_shared_rsp_*`，自闭环。

```verilog
shared_mem shared_mem(
  .core_req_valid_i       (pipe_shared_req_valid         ),
  .core_req_ready_o       (shared_mem_core_req_ready     ),
  ...
  .core_rsp_valid_o       (shared_mem_core_rsp_valid     ),
  ...
```

这就是「shared_memory 不参与仲裁」的源码证据。记住这条结论，后面 4.2～4.3 讨论的「两路」永远指 icache（index 0）和 dcache（index 1）。

> 关于 icache 的对外请求字段：icache 是只读的取指，所以它的 A 通道字段在 `sm_wrapper` 里被**硬编码**成固定值——`opcode = 3'h4`（TileLink 的 Get）、`mask` 全 1、`param` 不关心：

[src/gpgpu_top/sm/sm_wrapper.v:L247-L250](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L247-L250)

```verilog
assign icache_mem_req_a_opcode = 3'h4;                              // Get
assign icache_mem_req_a_data   = {`DCACHE_BLOCKWORDS*`XLEN{1'h0}};  // 无数据
assign icache_mem_req_a_mask   = {`DCACHE_BLOCKWORDS*`BYTESOFWORD{1'h1}}; // 全掩码
assign icache_mem_req_a_param  = 3'h0; //Dont care
```

#### 4.1.4 代码实践

**实践目标**：亲手验证「只有两路进仲裁器，shared_mem 不在其中」。

**操作步骤**：
1. 打开 `src/gpgpu_top/sm/sm_wrapper.v`。
2. 搜索 `l1cache_arb l1cache_arb`（约 430 行），确认其输入端口的拼接只有 `{dcache_*, icache_*}` 两个成员。
3. 搜索 `shared_mem shared_mem`（约 498 行），确认它的端口里**没有**任何 `mem_req_*` / `mem_rsp_*` 对外信号。
4. 用 Grep 在 `sm_wrapper.v` 里搜索 `mem_req_out`，确认对外存储接口在 `ifndef NO_CACHE` 分支里只有一组（一对 A/D）。

**需要观察的现象**：`l1cache_arb` 的所有数组型端口宽度都是 `NUM_CACHE_IN_SM = 2` 路拼接；`shared_mem` 完全不出现这些对外端口。

**预期结果**：你会清楚地看到 icache 和 dcache 共享一组对外接口，而 shared_mem 是旁路的。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `NUM_CACHE_IN_SM` 改成 3，并试图把 shared_mem 也接进仲裁器，shared_mem 的请求语义上能不能直接复用 TileLink A 通道？

**参考答案**：不能直接复用。shared_mem 的「请求」是带 lane 掩码、按 bank 划分的本地读写，地址是本地偏移、没有 tag/set 的概念，也不存在「缺失向 L2 取」的语义（u6-l2）。TileLink A 通道是为「可能缺失、需要下层填充」的缓存访问设计的。把它们硬塞进同一个仲裁器在协议上没有意义——shared_mem 根本不需要离开 SM。

**练习 2**：icache 的 `a_opcode` 为什么固定是 `3'h4`？

**参考答案**：取指是只读操作。TileLink 里 Get（读）操作码为 4，icache 只会读、不会写，所以 opcode 恒为 Get；data 全 0（不带写数据）、mask 全 1（整块都要读）。

---

### 4.2 请求通路：固定优先级仲裁与数据选择

#### 4.2.1 概念说明

两路请求（icache、dcache）要复用一路 A 通道，仲裁器每拍必须决定「让哪一路走」。本项目用的是**固定优先级（fixed priority）**策略：约定一个固定的优先顺序，冲突时高优先级者先走。

具体到这里：index 0（icache）优先级**最高**，index 1（dcache）次之。取指优先于访存，是因为取指停顿会让整条流水线断流，代价通常比单条访存指令更大。

仲裁分两步：
1. **选谁（one-hot）**：`fixed_pri_arb` 在所有有效请求里选出最低位（最高优先级），给出 one-hot 的 grant。
2. **取数据（binary + MUX）**：`one2bin` 把 one-hot 转成 binary 编号，再用这个编号当 MUX 的选择下标，把选中那一拍的 opcode/addr/data/mask/source 等字段接到对外 A 通道。

#### 4.2.2 核心流程

请求方向的处理流程（每一拍都是纯组合逻辑）：

```
mem_req_in_valid_i[1:0]  (dcache_valid, icache_valid)
        │
        ▼
   fixed_pri_arb  ──▶  mem_req_in_valid_oh[1:0]   (one-hot，最低有效位为 1)
        │
        ▼
     one2bin      ──▶  mem_req_in_valid_bin[0]    (binary: 0 选 icache, 1 选 dcache)
        │
        ├──▶ mem_req_out_valid_o  = in_valid[bin]
        ├──▶ mem_req_out_a_*      = in_a_*[以 bin 为下标切片]   (一个大 MUX)
        │
   反压 ready：
        in_ready[0] (icache) = out_ready
        in_ready[1] (dcache) = !in_valid[0] && out_ready   ← 只有 icache 没请求时 dcache 才能拿到 ready
```

注意 ready 的构造方式直接体现了固定优先级：icache（index 0）只要出口 ready 就能拿到 ready；dcache（index 1）必须**前面所有路都没有请求**（`!(|mem_req_in_valid_i[0:0])`，即 icache 没请求）且出口 ready 才能拿到 ready。

#### 4.2.3 源码精读

仲裁器主体（请求侧的反压 ready）：

[src/gpgpu_top/sm/l1cache_arb.v:L63-L70](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache_arb.v#L63-L70) —— ready 的优先级构造。

```verilog
assign mem_req_in_ready_o[0] = mem_req_out_ready_i;                       // icache：出口 ready 即可

genvar j;
generate
  for(j=1;j<`NUM_CACHE_IN_SM;j=j+1) begin:B2
    assign mem_req_in_ready_o[j] = !(|mem_req_in_valid_i[j-1:0]) && mem_req_out_ready_i;  // dcache：还得 icache 没请求
  end
endgenerate
```

选出 winning port 的字段并接到对外 A 通道（一个按 `mem_req_in_valid_bin` 下标切片的 MUX）：

[src/gpgpu_top/sm/l1cache_arb.v:L72-L78](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache_arb.v#L72-L78)

```verilog
assign mem_req_out_valid_o    = mem_req_in_valid_i[mem_req_in_valid_bin];
assign mem_req_out_a_opcode_o = mem_req_in_a_opcode_i[(3*(mem_req_in_valid_bin+1)-1)-:3];
assign mem_req_out_a_param_o  = mem_req_in_a_param_i [(3*(mem_req_in_valid_bin+1)-1)-:3];
assign mem_req_out_a_addr_o   = mem_req_in_a_addr_i  [(`XLEN*(mem_req_in_valid_bin+1)-1)-:`XLEN];
assign mem_req_out_a_data_o   = mem_req_in_a_data_i  [(`DCACHE_BLOCKWORDS*`XLEN*(mem_req_in_valid_bin+1)-1)-:(`DCACHE_BLOCKWORDS*`XLEN)];
assign mem_req_out_a_mask_o   = ...;
assign mem_req_out_a_source_o = mem_req_arb_a_source[(`D_SOURCE*(mem_req_in_valid_bin+1)-1)-:`D_SOURCE];
```

> 关于 Verilog 的 `-:` 切片语法：`[hi -: W]` 表示从高位 `hi` 开始向下取 `W` 位。这里的 `hi = W*(bin+1)-1`，正是第 `bin` 路那一段字段的最高位，所以一切片就精确取到第 `bin` 路的字段。

底层是两个公共单元。`fixed_pri_arb` 用「前缀或」屏蔽掉低位已有请求的高位，从而只让最低有效位通过：

[src/common_cell/fixed_pri_arb.v:L24-L27](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/fixed_pri_arb.v#L24-L27)

```verilog
assign pre_req = {(req[ARB_WIDTH-2:0] | pre_req[ARB_WIDTH-2:0]),1'h0}; // 前缀：bit i 为 1 当且仅当存在更低位的请求
assign grant   = req & (~pre_req);                                     // 只保留最低有效位
```

`one2bin` 把 one-hot 转成 binary（这里 `BIN_WIDTH = NUM_CACHE_DEPTH = 1`）：

[src/common_cell/one2bin.v:L29-L44](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/common_cell/one2bin.v#L29-L44) —— `oh[i] ? i : 0` 再按位归约。

仲裁器顶层把这两个单元串起来：

[src/gpgpu_top/sm/l1cache_arb.v:L87-L100](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache_arb.v#L87-L100)

```verilog
fixed_pri_arb #(.ARB_WIDTH(`NUM_CACHE_IN_SM)) mem_req_arb(
  .req  (mem_req_in_valid_i ),
  .grant(mem_req_in_valid_oh)
);

one2bin #(.ONE_WIDTH(`NUM_CACHE_IN_SM), .BIN_WIDTH(`NUM_CACHE_DEPTH)) one2bin(
  .oh (mem_req_in_valid_oh ),
  .bin(mem_req_in_valid_bin)
);
```

#### 4.2.4 代码实践

**实践目标**：推演「icache 和 dcache 同时请求」时谁先走。

**操作步骤**：
1. 假设某拍 `mem_req_in_valid_i = 2'b11`（icache 和 dcache 同时有效），且 `mem_req_out_ready_i = 1`。
2. 手动套用 `fixed_pri_arb`：`pre_req = {(req[0] | pre_req[0]), 0} = {req[0], 0} = 2'b10`；`grant = 2'b11 & ~2'b10 = 2'b01`。
3. `one2bin` 把 `2'b01` 转成 `bin = 1'd0`。
4. 于是 `mem_req_in_ready_o[0] = 1`（icache 拿到 ready，握手成功），`mem_req_in_ready_o[1] = !(|2'b11[0:0]) && 1 = !1 && 1 = 0`（dcache 被反压）。
5. 对外 `mem_req_out_*` 字段全部取 index 0 即 icache 的字段。

**需要观察的现象**：当两者同时请求，icache 这一拍成功发出，dcache 必须等到下一拍 icache 没请求时才能发出。

**预期结果**：取指请求永远不会被访存请求「插队」。

#### 4.2.5 小练习与答案

**练习 1**：固定优先级仲裁 vs 轮询（round-robin）仲裁，本项目这里为什么选固定优先级？

**参考答案**：取指停顿会直接断流整条流水线（所有 warp 都没法前进），而单条访存指令的延迟通常靠 warp 切换隐藏（见 u3-l4）。因此给 icache 绝对优先级、避免取指被访存阻塞，整体吞吐更优。固定优先级实现也更简单（纯组合、无状态）。代价是 dcache 在 icache 持续请求时可能「饿死」，但取指请求不会长时间持续占满，所以实际可接受。

**练习 2**：`mem_req_in_valid_bin` 是几位宽？为什么？

**参考答案**：`NUM_CACHE_DEPTH = $clog2(NUM_CACHE_IN_SM) = $clog2(2) = 1` 位。两路只需 1 位二进制就能区分（0=icache，1=dcache）。

---

### 4.3 source 标签编码与响应路由

#### 4.3.1 概念说明

请求复用成一路发出去后，L2 的响应会从同一条 D 通道回来。仲裁器必须知道**每个响应该还给 icache 还是 dcache**——但 D 通道里并没有专门的「给谁」字段。

解决办法是**借用 TileLink 的 source 字段做标签**：

- **发请求时**：把「我来自哪个 cache（cache_id）」拼接进 source 的高位。source 就成了一个回信地址。
- **收响应时**：L2 会原样回填这个 source（TileLink 规定响应 source 等于请求 source），于是仲裁器只需看 source 的高位就知道响应该路由给谁。

这是 u7-l1 会详细讲的「source 编码」在 L1 这一层的第一站。

#### 4.3.2 核心流程

source 位宽关系（关键公式）：

\[
\text{A\_SOURCE} = 3 + \text{DCACHE\_ENTRY\_DEPTH} + \text{DCACHE\_SETIDXBITS}
\]

\[
\text{D\_SOURCE} = \text{NUM\_CACHE\_DEPTH} + \text{A\_SOURCE}
\]

也就是说，D 通道的 source 比 A 通道多了 `NUM_CACHE_DEPTH` 位（这里是 1 位），这 1 位就是 cache_id。请求方向把 cache_id 拼到最高位；响应方向取最高 1 位做路由。

请求 source 打包：

```
输出 source[D_SOURCE-1:0] = { cache_id[NUM_CACHE_DEPTH-1:0], 输入 source[A_SOURCE-1:0] }
```

响应路由（解复用）：

```
若 D_source 的高 NUM_CACHE_DEPTH 位 == i，则 mem_rsp_out_valid_o[i] = 1
mem_rsp_in_ready_o          = mem_rsp_out_ready_i[D_source 的高 NUM_CACHE_DEPTH 位]
```

#### 4.3.3 源码精读

先看位宽定义：

[src/define/define.v:L107-L113](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/define/define.v#L107-L113)

```verilog
`define NUM_CACHE_DEPTH $clog2(`NUM_CACHE_IN_SM)              // = 1
`define D_SOURCE (`NUM_CACHE_DEPTH+3+`DCACHE_ENTRY_DEPTH+`DCACHE_SETIDXBITS)
`define A_SOURCE (3+`DCACHE_ENTRY_DEPTH+`DCACHE_SETIDXBITS)   // 比 D_SOURCE 少了 NUM_CACHE_DEPTH 位
```

请求方向：用一个 generate 为每一路把 cache_id（即循环变量 `i`）拼到 source 最高位：

[src/gpgpu_top/sm/l1cache_arb.v:L55-L61](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache_arb.v#L55-L61)

```verilog
genvar i;
generate for(i=0;i<`NUM_CACHE_IN_SM;i=i+1) begin:B1
  // 请求 source：在输入 A_SOURCE 前面拼上 cache_id i，组成 D_SOURCE 位
  assign mem_req_arb_a_source[(`D_SOURCE*(i+1)-1)-:`D_SOURCE] = {i,mem_req_in_a_source_i[(`A_SOURCE*(i+1)-1)-:`A_SOURCE]};
  // 响应有效：若响应 source 的高 NUM_CACHE_DEPTH 位等于 i，则该路有效
  assign mem_rsp_out_valid_o[i] = (i == mem_rsp_in_d_source_i[(`D_SOURCE-1)-:`NUM_CACHE_DEPTH]) ? mem_rsp_in_valid_i : 1'h0;
end
endgenerate
```

响应方向：opcode/addr/data 是公共的（一次响应只属于一个 cache），所以广播给所有路；但 source 只取低 `A_SOURCE` 位广播（剥掉最高位的 cache_id，因为各 cache 内部只认 A_SOURCE 宽的 source）：

[src/gpgpu_top/sm/l1cache_arb.v:L80-L85](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache_arb.v#L80-L85)

```verilog
assign mem_rsp_out_d_opcode_o = {`NUM_CACHE_IN_SM{mem_rsp_in_d_opcode_i}};              // 广播
assign mem_rsp_out_d_addr_o   = {`NUM_CACHE_IN_SM{mem_rsp_in_d_addr_i  }};
assign mem_rsp_out_d_data_o   = {`NUM_CACHE_IN_SM{mem_rsp_in_d_data_i  }};
assign mem_rsp_out_d_source_o = {`NUM_CACHE_IN_SM{mem_rsp_in_d_source_i[`A_SOURCE-1:0]}}; // 取低 A_SOURCE 位广播
assign mem_rsp_in_ready_o     = mem_rsp_out_ready_i[mem_rsp_in_d_source_i[`D_SOURCE-1-:`NUM_CACHE_DEPTH]]; // 按 source 路由 ready
```

注意：虽然 opcode/addr/data 广播给了两路，但每一路的 `valid` 已经被 4.3.3 第一段的 generate 过滤成「只有目标 cache 那一路为 1」，所以非目标 cache 即使看到数据也不会握手。

#### 4.3.4 代码实践

**实践目标**：跟踪一个 dcache 的响应如何被精确路由回 dcache 而不误触发 icache。

**操作步骤**：
1. 假设 dcache 发了一个请求，请求方向 source 被打包成 `{1'b1, a_source}`（cache_id=1 表示 dcache）。
2. L2 处理完后，D 通道回来的 `d_source = {1'b1, a_source}`（高位仍为 1）。
3. 在 generate 里：对 `i=0`（icache），`1 == 1'b1 ?` 不成立，`mem_rsp_out_valid_o[0] = 0`；对 `i=1`（dcache），`1 == 1'b1` 成立，`mem_rsp_out_valid_o[1] = mem_rsp_in_valid_i`。
4. `mem_rsp_in_ready_o = mem_rsp_out_ready_i[1]`（取 dcache 的 ready）。

**需要观察的现象**：icache 的 `valid` 保持 0，dcache 的 `valid` 跟随外部 `valid`；ready 也只取 dcache 一路的。

**预期结果**：响应被精确地送给 dcache，icache 完全不受影响。

#### 4.3.5 小练习与答案

**练习 1**：为什么响应的 source 要剥掉最高位（只取低 `A_SOURCE` 位）再广播给各 cache？

**参考答案**：各 cache 内部维护的 MSHR / 表项是按 `A_SOURCE` 宽的 source 寻址的（`A_SOURCE` 里已经包含 entry 号和 set 号）。最高位的 cache_id 是 `l1cache_arb` 自己加的「路由标签」，cache 内部不认识、也不需要。所以广播时必须剥掉它，恢复成 cache 原生宽度的 source。

**练习 2**：如果 L2 回的响应 source 高位是一个既不是 0 也不是 1 的值（比如位宽扩大后出现），会发生什么？

**参考答案**：那么 `mem_rsp_out_valid_o` 所有路都为 0（generate 里没有 `i` 能匹配），`mem_rsp_in_ready_o` 会索引到 `mem_rsp_out_ready_i` 的一个未定义/越界位置。这是一个不应发生的协议违例——正常情况下 L2 必须原样回填请求时的 source，所以高位只会是合法的 cache_id。

---

### 4.4 cache_invalid 与 flush 冲刷协同

#### 4.4.1 概念说明

前面三节讲的是「正常运行时」的仲裁。但 workgroup 跑完后还有一件大事：**冲刷缓存、回报主机**。

回顾 u1-l5 讲过的控制流：主机下发 workgroup → SM 执行 → warp 全完成产生 `wg_done` → **此时不能立即回报主机**，因为 D-cache 里可能还有未写回 L2 的脏数据，主机若直接读 L2 会读到旧值。所以必须：

1. 触发缓存冲刷：把 D-cache 的脏数据写回、把 I-cache 的旧指令无效掉。
2. 等 L2 把所有冲刷引起的事务处理完。
3. 才拉起 `host_rsp_valid_o`，告诉主机「可以读了」。

`cache_invalid` 就是这条链上的触发信号。它由顶层的 `wg_done` 产生，下发给 SM，SM 内部分别处理 icache 和 dcache。

#### 4.4.2 核心流程

整条冲刷握手链：

```
warp 全完成 ──▶ wg_done
                 │
                 ▼  (GPGPU_top)
        cache_invalid[0] = wg_done    （注意：只触发 SM[0]，有 TODO）
        is_flushing <= 1
                 │
                 ▼  (下发给 SM 的 cache_invalid_i)
        ┌────────┴────────┐
        ▼                 ▼
   icache 处理：       dcache 处理：
   invalid_i 直接      先等 lsu_mshr_is_empty
   清空有效位          再发 invalidate 请求(opcode=3,param=0) 进 dcache
                       （这个 invalidate 请求也会经 l1cache_arb 对外发 GET/PUTFULL）
                 │
                 ▼  (dcache 处理完 flush，向 L2 发出写回/无效事务)
              L2 处理完 ──▶ l2cache_finish_issue
                 │
                 ▼  (GPGPU_top)
        is_flushing <= 0
        host_rsp_valid_o = l2cache_finish_issue && is_flushing   （拉起一拍）
```

dcache 的失效请求会用到一个特殊 opcode。根据 `dcache_control.v`：

[src/gpgpu_top/sm/l1cache/dcache/dcache_control.v:L33-L35](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/l1cache/dcache/dcache_control.v#L33-L35)

```verilog
assign is_flush      = ((opcode==3'b011) && (param==4'b0001)) ? 'd1 : 'd0;  // flush
assign is_invalidate = ((opcode==3'b011) && (param==4'b0000)) ? 'd1 : 'd0;  // invalidate
assign is_wait_mshr  = ((opcode==3'b011) && (param==4'b0010)) ? 'd1 : 'd0;  // wait_mshr
```

即 `opcode=3, param=0` 是 invalidate（无效化，不回写）、`opcode=3, param=1` 是 flush（冲刷、回写脏数据）。

#### 4.4.3 源码精读

顶层产生 `cache_invalid` 与 `host_rsp_valid_o`：

[src/gpgpu_top/GPGPU_top.v:L250-L270](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/GPGPU_top.v#L250-L270)

```verilog
reg is_flushing;
//TODO: cache_invalid can't multi SM
assign cache_invalid    = {wg_done,{(`NUMBER_CU-1){1'b0}}};   // 只有 SM[0] 位被 wg_done 驱动
assign host_rsp_valid_o = l2cache_finish_issue && is_flushing;

always@(posedge clk or negedge rst_n) begin
  if(!rst_n)                  is_flushing <= 'd0;
  else if(wg_done)            is_flushing <= 1'b1;     // wg_done 起进入冲刷
  else if(l2cache_finish_issue) is_flushing <= 'd0;    // L2 处理完，退出冲刷
  else                        is_flushing <= is_flushing;
end
```

注意第 252 行的 TODO「cache_invalid can't multi SM」——目前 `wg_done` 只驱动 `cache_invalid[0]`，即只冲刷 SM[0] 的缓存。多 SM 场景下这是个**待完善的限制**（见 u1-l5 提到的同类问题）。

`cache_invalid_i` 进入 `sm_wrapper` 后分两路处理。

**icache 一路**：直接接到 icache 的 `invalid_i` 端口，整表清空有效位：

[src/gpgpu_top/sm/sm_wrapper.v:L465-L469](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L465-L469)

```verilog
instruction_cache icache(
  .clk               (clk              ),
  .rst_n             (rst_n            ),
  .invalid_i         (cache_invalid_i  ),   // 直接无效化整个 icache
  ...
```

**dcache 一路**：不能立即无效化，因为可能还有在途的访存请求（LSU MSHR 里有未完成事务）。所以要等 `lsu_mshr_is_empty`，然后用一个寄存器 `cache_invalid_reg` 暂存冲刷意图，等 LSU 空了再生成一次 invalidate 请求（opcode=3）注入 dcache 的输入：

[src/gpgpu_top/sm/sm_wrapper.v:L161-L186](https://github.com/THU-DSP-LAB/ventus-gpgpu-verilog/blob/192d1e054d8628fe188894927c0e1976f4c25cde/src/gpgpu_top/sm/sm_wrapper.v#L161-L186)

```verilog
wire lsu_mshr_is_empty  ;
wire cache_invalid_valid;
reg  cache_invalid_reg;

always@(posedge clk or negedge rst_n) begin
  if(!rst_n)              cache_invalid_reg <= 'd0;
  else if(cache_invalid_i)cache_invalid_reg <= 'd1;      // 锁存冲刷意图
  else if(cache_invalid_valid) cache_invalid_reg <= 'd0; // 发出后清掉
  else                    cache_invalid_reg <= cache_invalid_reg;
end

assign cache_invalid_valid        = cache_invalid_reg && lsu_mshr_is_empty;            // 等 LSU 空了才有效
assign pipe_dcache_req_valid_comb = lsu2d_q_deq_valid || cache_invalid_valid;          // 普通访存 或 失效请求
assign pipe_dcache_req_opcode_comb= cache_invalid_valid ? 'd3 : lsu2d_q_deq_opcode;    // 失效时 opcode=3
assign pipe_dcache_req_param_comb = cache_invalid_valid ? 'd0 : lsu2d_q_deq_param;     // param=0 → invalidate
```

也就是说：dcache 的失效请求和普通 LSU 访存请求**走同一个输入端口**（靠 `valid_comb / opcode_comb / param_comb` 复用），优先级是「失效请求插队」。这个请求进入 dcache 后，dcache 内部状态机会发起向 L2 的写回/无效事务，这些事务再经 `l1cache_arb` 对外发出——所以**冲刷流量和正常访存流量共用同一条 4.2 节讲到的仲裁通路**。

#### 4.4.4 代码实践

**实践目标**：定位 `cache_invalid` 从产生到消化掉的完整代码路径。

**操作步骤**（源码阅读型，无需运行）：
1. 在 `src/gpgpu_top/GPGPU_top.v` 找到 `assign cache_invalid`（约 253 行），确认它由 `wg_done` 驱动、且只驱动 SM[0]。同时找到 `is_flushing` 的状态机与 `host_rsp_valid_o` 的赋值。
2. 搜索 `cache_invalid_i` 在 `sm_wrapper.v` 里的两处消费：icache 的 `invalid_i`、以及 `cache_invalid_reg` 的置位条件。
3. 追踪 `cache_invalid_valid` 如何依赖 `lsu_mshr_is_empty`（这个信号由 `pipe` 输出，见 `sm_wrapper.v` 第 393 行 `.lsu_mshr_is_empty_o`），理解「为什么要等 LSU 空」。
4. 确认 `pipe_dcache_req_opcode_comb` 在失效时为 `'d3`，对照 `dcache_control.v` 第 34 行确认这是 invalidate。

**需要观察的现象**：dcache 的冲刷是「延迟生效」的——`cache_invalid_i` 来了之后不是立刻动作，而是先锁存、等 LSU 排空。

**预期结果**：你能画出从 `wg_done` 到 `host_rsp_valid_o` 的完整时序：`wg_done → cache_invalid_i → (等 LSU 空) → dcache invalidate → L2 写回/无效 → l2cache_finish_issue → host_rsp_valid_o`。

> 待本地验证：实际波形中，`cache_invalid_i` 拉高到 `lsu_mshr_is_empty` 拉高之间的间隔取决于当前在途访存请求数，建议在 tc_vecadd 仿真里用 Verdi 抓 `wg_done`、`cache_invalid_i`、`lsu_mshr_is_empty`、`l2cache_finish_issue`、`host_rsp_valid_o` 五个信号对照。

#### 4.4.5 小练习与答案

**练习 1**：为什么 dcache 的 `cache_invalid_i` 不能像 icache 那样直接生效，而要等 `lsu_mshr_is_empty`？

**参考答案**：icache 的取指请求是「读且丢弃型」——无效化时丢弃在途取指不会丢数据。但 dcache 后面挂着 LSU 的 MSHR，里面有正在等待的 load/store 事务；如果在它们完成前就无效化 dcache，已发出的 load 会拿到错乱的结果、未写回的 store 会丢失数据。所以必须等 LSU MSHR 排空（`lsu_mshr_is_empty`）后才能安全失效。

**练习 2**：如果同时有普通访存请求（`lsu2d_q_deq_valid`）和 `cache_invalid_valid`，谁优先？为什么这样设计是安全的？

**参考答案**：`cache_invalid_valid` 优先（从 `pipe_dcache_req_opcode_comb = cache_invalid_valid ? 'd3 : lsu2d_q_deq_opcode` 可见，一旦失效有效，opcode 被强制改成 3）。这是安全的，因为 `cache_invalid_valid` 本身就要求 `lsu_mshr_is_empty` 为真——此时已没有在途访存，普通队列里即使还有请求，让失效先走也不会破坏数据一致性。

---

## 5. 综合实践

**任务**：把本讲四个最小模块串起来，画出一张完整的「SM 对外存储通路 + 冲刷」时序与连线图，并回答三个问题。

**步骤**：

1. **画连线图**。以 `sm_wrapper.v` 的例化为依据，画出：
   - `icache`、`dcache` 如何经 `l1cache_arb` 复用成一组 `mem_req_*` / `mem_rsp_*`；
   - `shared_mem` 如何旁路直连 `pipe`（标注「不经仲裁、不出 SM」）；
   - `cache_invalid_i` 如何分别进 icache（直接）和 dcache（经 `cache_invalid_reg` + `lsu_mshr_is_empty`）。
2. **标注 source 位宽**。在图上标出请求 source 被打包成 `D_SOURCE` 位（最高 `NUM_CACHE_DEPTH` 位是 cache_id），响应 source 被剥成 `A_SOURCE` 位广播。
3. **回答三个问题**：
   - 若 icache 和 dcache 同一拍都请求，谁先走？（依据 4.2）
   - 一个 dcache 的响应会不会误触发 icache？为什么？（依据 4.3）
   - `wg_done` 之后到 `host_rsp_valid_o` 之间，`l1cache_arb` 上会多出什么流量？（依据 4.4）

**预期产出**：一张连线图 + 三段简短回答。

**参考答案要点**：
1. icache 先走（index 0 优先级最高，dcache 的 ready 被 `!icache_valid` 反压）。
2. 不会。响应 source 的高位 cache_id 会与每一路的 `i` 比较，只有匹配路（dcache，i=1）的 `valid` 被置 1；icache（i=0）的 `valid` 为 0。
3. dcache 失效请求（opcode=3）会引发脏块写回（TileLink PUTFULL）等事务，这些流量和正常访存一样经 `l1cache_arb` 对外发出，直到 L2 处理完产生 `l2cache_finish_issue`。

## 6. 本讲小结

- `l1cache_arb` 是 SM 内部 L1 缓存与对外存储接口之间的纯组合仲裁器，规模由 `NUM_CACHE_IN_SM = 2` 决定，**只仲裁 icache 和 dcache 两路**。
- **shared_memory 不参与仲裁**——它命中即返回、永不缺失、不出 SM，直接挂在 `pipe` 上。这是本讲最重要的认知纠正。
- 请求方向用**固定优先级**（icache > dcache）：`fixed_pri_arb` 选出最低有效位（one-hot），`one2bin` 转 binary，再用一个按位切片的大 MUX 把选中路的字段接到对外 A 通道；ready 的优先级构造保证 icache 不会被 dcache 插队。
- 响应方向用 **source 字段做路由标签**：请求时把 cache_id 拼进 source 高位（`D_SOURCE = NUM_CACHE_DEPTH + A_SOURCE`），响应时按 source 高位解复用回正确的 cache，并剥掉标签位恢复 `A_SOURCE` 宽度。
- `cache_invalid` 由顶层 `wg_done` 产生（目前仅 SM[0]，有 TODO）：icache 直接整表无效，dcache 等 `lsu_mshr_is_empty` 后注入 `opcode=3/param=0` 的 invalidate 请求，最终 L2 处理完发 `l2cache_finish_issue`，配合 `is_flushing` 拉起 `host_rsp_valid_o`。
- 冲刷流量与正常访存流量**共用同一条 `l1cache_arb` 仲裁通路**对 L2 发出。

## 7. 下一步学习建议

- **u7-l1 TileLink 协议基础与操作码**：本讲的 source/opcode/param 字段都属 TileLink 语义，下一讲会系统讲解 A/D 通道与 GET/PUTFULL/ACQUIRE 等操作码，建议紧接着学。
- **u7-l3 cluster 到 L2 的互联**：本讲停在 `mem_req_*` 离开 SM 的位置；离开 SM 后请求进入 `sm2cluster_arb → l2_distribute → cluster_to_l2_arb` 三级互联到达 L2，是自然的后续。
- **延伸阅读**：回看 u6-l1（dcache 与 MSHR）和 u6-l2（shared_memory），对照体会「为什么 dcache 要进仲裁、shared_memory 不要」这一设计区分的深层原因。

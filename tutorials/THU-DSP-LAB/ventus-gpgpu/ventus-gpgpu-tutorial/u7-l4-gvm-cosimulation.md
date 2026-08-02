# GVM 协同仿真与对拍

## 1. 本讲目标

本讲讲解 Ventus 的 **GVM（GPGPU Vector Model）协同仿真**机制。读完本讲后，你应当能够：

- 说清**为什么**要在 RTL 仿真里再跑一个参考模型（SPIKE ISA 模拟器），GVM 到底在比对什么。
- 理解 `RTL_GVM_ENABLED` 与 `ENABLE_GVM` 两个开关分别作用在 Chisel 层和 C++ 层，以及二者如何配合。
- 看懂 `GvmDutApi.scala`（一组 `ExtModule`）与 `gvm_dpic.cpp`（一组 `DPI-C` 函数）如何把 RTL 内部信号“钓”到 C++ 全局变量里。
- 掌握 `gvm_t::getDut()` 如何把零散的原始信号组织成“按软件 warp 索引的指令序列 + 标量寄存器快照”。
- 掌握 `gvm_t::gvmStep()` 如何以**指令退休（retire）**为粒度同步步进 SPIKE，并做“逐条指令比对”与“整堆寄存器比对”。

本讲是 u7-l3（Verilator 仿真框架）的延续——u7-l3 讲的是仿真主循环 `step()`，本讲讲的是挂在 `step()` 末尾、与主循环同周期的“对拍引擎”。

## 2. 前置知识

| 概念 | 通俗解释 |
|------|----------|
| **DPI-C** | SystemVerilog 与 C 互操作的标准接口。Verilog 侧 `import "DPI-C"` 声明一个 C 函数，仿真器在运行时直接调用它，把硬件信号当参数传给 C。 |
| **参考模型 / ISA 模拟器（SPIKE）** | 用 C++ 写的、功能正确但不在乎时序的 RISC-V 指令解释器。它逐条取指、译码、执行、写回，是“黄金参考”。Ventus 的 SPIKE 改造版以动态库 `libgvmref.so` 形式提供。 |
| **指令退休（retire）** | 一条指令真正完成、其写回结果对架构状态永久生效的时刻。乱序执行的 CPU/GPU 内部可以乱序完成，但“按程序顺序退休”。 |
| **软硬件 warp 身份** | `hardware_warp_id` 是 SM 内部 0~num_warp-1 的私有编号（会随 warp 结束/新 warp 到来被复用）；`(software_wg_id, software_warp_id)` 才是全局唯一、与 SPIKE 对应的软件身份。 |
| **标量寄存器堆交织** | 见 u4-l4：标量寄存器按 `(warp 基址 + regIdx)` 散列到多个 bank，GVM 需要逆向“解交织”才能读出某个 warp 的连续寄存器。 |
| **`step()` 主循环** | 见 u7-l3：每调一次 `step()` 推进半个时钟，两次 `step()` 为一个完整时钟周期。 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `ventus/src/GvmDutApi.scala` | 用 Chisel `ExtModule` 内联生成 6 个 SystemVerilog 模块，每个模块在时钟沿把一组 RTL 信号通过 DPI-C 回调送给 C++。 |
| `sim-verilator/gvm_dpic.cpp` | DPI-C 函数的 C++ 实现，把收到的信号塞进全局变量（`g_xxx_data`）。 |
| `sim-verilator/gvm_global_var.hpp` / `.cpp` | 定义并实例化那一组“RTL→C++”的全局缓冲区与结构体。 |
| `sim-verilator/gvm.hpp` / `gvm_structs.hpp` | `gvm_t` 类声明及其使用的结构体（`insn_t`、`dut_active_warp_t` 等）。 |
| `sim-verilator/gvm.cpp` | `gvm_t` 的实现：`getDut()` 收集 DUT 状态、`gvmStep()` 步进 REF 并比对。 |
| `sim-verilator/gvmref_interface.h` | SPIKE 参考模型对外暴露的 C API 声明（`gvmref_step`、`gvmref_get_xreg` 等）。 |
| `sim-verilator/gvm_care_insns.cpp` | 各类“关心”指令表（标量/向量/屏障/浮点），用掩码+值描述。 |
| `sim-verilator/ventus_rtlsim_impl.cpp` / `.hpp` | 仿真器主类，持有 `gvm_t gvm` 成员，在 `step()` 末尾调用 `getDut()`/`gvmStep()`。 |
| 各流水级 Scala（`GPGPU_top.scala`、`pipe.scala`、`ibuffer.scala`、`writeback.scala`、`warp_schedule.scala`） | 在硬件关键节点例化 `GvmDut*` 钩子，受 `GVM_ENABLED` 编译开关控制。 |

## 4. 核心概念与源码讲解

### 4.1 GVM 协同仿真的整体思想与开关

#### 4.1.1 概念说明

单跑 RTL 仿真（u7-l3 那条路）只能告诉你“程序跑完了、内存里的结果对不对”。但“结果对”不等于“每一步都对”——也许某条指令算错了，只是恰好被后面的指令覆盖、或被宽松的浮点误差掩盖。等程序变大、变复杂，这类**隐藏 bug** 极难定位。

GVM 的思路是**双模型并行 + 逐指令对拍**：

1. 用**同一份 kernel 元数据**同时驱动 RTL（DUT）与 SPIKE（REF）。
2. DUT 跑自己的乱序流水线；REF 严格按程序顺序逐条执行。
3. 每个时钟周期，GVM 把 DUT 内部“派发了哪些指令、写回了哪些寄存器”等信号抽出来，重组出 DUT 视角下的“按程序顺序排好的指令流”。
4. 当 DUT 的某段指令确认可以**按序退休**时，GVM 让 REF 同样步进相同条数的指令，然后逐条比对的写回结果，并整体比对的标量寄存器堆快照。
5. 任何不一致立即 `logger->error` 并打印 `sm_id / hardware_warp_id / software_wg_id / software_warp_id / pc / insn` 等定位信息。

关键难点在于**身份翻译**：DUT 内部只知道 `sm_id + hardware_warp_id`，而 REF 只认识 `(software_wg_id, software_warp_id)`，且标量寄存器在 DUT 里是交织存储的。GVM 的核心工作就是搭起这两套身份与两套存储之间的桥。

#### 4.1.2 核心流程

```
        ┌─────────────── 同一份 kernel metadata ───────────────┐
        ▼                                                      ▼
   ┌─────────┐  DPI-C 钩子 (GvmDutApi + gvm_dpic)        ┌──────────┐
   │ RTL DUT │ ───────────────────────────────────────▶ │ gvm_t    │
   │ (Verilog)│  g_cta2warp_data / g_insn_dispatch_data │  (C++)   │
   │         │  g_xreg_wb_data / g_vreg_wb_data ...     │          │
   └─────────┘                                          │  getDut()│ ── 重组 DUT 视图
        │                                               │  gvmStep │ ── 步进 REF + 比对
        │ host_req/host_rsp                             └────┬─────┘
        ▼                                                    │ gvmref_* API
   ┌─────────────────┐    fw_vt_start / fw_vt_copy_to_dev  ▼
   │ 仿真 driver     │ ──────────────────────────────── ┌──────────┐
   │ (cta_sche_wrapper)│                                │ SPIKE REF│
   └─────────────────┘                                  │(libgvmref)│
                                                        └──────────┘
```

每个时钟周期（`step()` 的负半周）执行一次 `getDut()` 收集 + `gvmStep()` 比对。

#### 4.1.3 源码精读

**两个开关分属两层。** Chisel 层的 `GVM_ENABLED` 决定要不要例化硬件钩子：

[ventus/src/top/parameters.scala:14](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/parameters.scala#L14) — `GVM_ENABLED` 读环境变量 `RTL_GVM_ENABLED`，默认 `false`。当为 `true` 时，`GvmDut*` 钩子才被综合进 Verilog。

C++ 层的 `ENABLE_GVM` 决定仿真器要不要链接对拍逻辑：

- [sim-verilator/verilate.mk:10](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/verilate.mk#L10) — 普通仿真默认 `export RTL_GVM_ENABLED = false`（不生成钩子，也不比对）。
- [sim-verilator/gvm.mk:12](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.mk#L12) — GVM 专用构建 `export RTL_GVM_ENABLED = true`，使 Chisel 侧生成钩子；[gvm.mk:138](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.mk#L138) 加 `-DENABLE_GVM=1` 启用 C++ 对拍代码；[gvm.mk:185](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.mk#L185) 链接 `-lgvmref`（SPIKE 动态库）。

> 二者必须同时开：只开 `ENABLE_GVM` 而 Verilog 里没有钩子，C++ 收不到信号；只生成钩子而不开 `ENABLE_GVM`，`gvm_t` 根本不存在。

**`gvm_t` 是仿真器主类的成员，由 `#ifdef ENABLE_GVM` 包裹**：

[sim-verilator/ventus_rtlsim_impl.hpp:33-35](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.hpp#L33-L35) — `ventus_rtlsim_t` 结构体里持有 `gvm_t gvm;`，与 `dut`、`cta`、`pmem` 并列。

[sim-verilator/ventus_rtlsim_impl.cpp:343-348](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim_impl.cpp#L343-L348) — 在 `step()` 末尾，当 `contextp->time() % 2 == 1`（即每个完整时钟周期的负半周）调用一次 `gvm.getDut()` 和 `gvm.gvmStep()`。这与 u7-l3 讲的“两次 `step()` 为一周期、`HALF_CYCLE_TIME=5`”一致：时间戳为奇数（5、15、25……）的半周各触发一次对拍。

**REF 由同一份 metadata 驱动。** [sim-verilator/ventus_rtlsim.cpp:108-110](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim.cpp#L108-L110) 把 `fw_vt_start`（启动 kernel）转发给 `gvmref_vt_start`；`fw_vt_copy_to_dev`、`fw_vt_dev_open` 同理。这些 `fw_vt_*` 在 [ventus_rtlsim.h](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/ventus_rtlsim.h) 中以 `DLL_PUBLIC` 导出，供外部 GVM driver 调用——即 driver 每向 DUT 派发一个 kernel，也同步把同一份 metadata 灌进 SPIKE。

#### 4.1.4 代码实践

**目标**：搞清两个开关的层级关系，不实际编译。

1. 打开 `sim-verilator/verilate.mk` 与 `sim-verilator/gvm.mk`，对比 `RTL_GVM_ENABLED` 与 `ENABLE_GVM` 的取值差异。
2. 在仓库根目录执行 `grep -rn "GVM_ENABLED" ventus/src` 与 `grep -rn "ENABLE_GVM" sim-verilator`，统计两类开关各出现在多少处。
3. **预期结果**：`GVM_ENABLED` 只出现在 Scala 文件里（决定硬件钩子是否综合），`ENABLE_GVM` 只出现在 C++/mk 文件里（决定对拍代码是否编译）。这验证了“硬件层 vs 软件层”的分工。

#### 4.1.5 小练习与答案

**练习 1**：如果只设置环境变量 `RTL_GVM_ENABLED=true` 却用普通 `make -j run`（走 `verilate.mk`）构建，会发生什么？
**答案**：Verilog 里会生成 `GvmDut*` 钩子并在仿真时调用 DPI-C 函数 `c_GvmDutXxx`，但这些函数（`gvm_dpic.cpp`）把数据写进全局变量后无人消费；同时 C++ 侧因 `ENABLE_GVM` 未定义，`gvm_t` 根本不存在，`getDut/gvmStep` 不被调用。结果是对拍不生效，且若 `gvm_dpic.cpp` 未参与编译还会出现链接错误。

**练习 2**：为什么 `getDut()/gvmStep()` 用 `time() % 2 == 1` 而不是每个 `step()` 都调？
**答案**：一次 `step()` 推进半个时钟（时间 +5）。每个完整时钟周期（两个半周）只需要对拍一次，否则同一拍的信号会被处理两遍。`time()%2==1` 选中负半周，正好每周期触发一次。

---

### 4.2 GvmDutApi 与 gvm_dpic：用 DPI-C 钓取 DUT 内部状态

本模块覆盖最小模块 **GvmDutApi**（含 **GvmDutCta2Warp**）与 **gvm_dpic**。

#### 4.2.1 概念说明

RTL 内部的信号（“第 3 号 SM 第 5 号 warp 刚写回了标量寄存器 x7=0x1234”）原本对 C++ 不可见。GVM 用一组“探测模块”解决这个问题：

- **`GvmDutApi.scala`** 里每个 `class GvmDutXxx` 是一个 Chisel `ExtModule`，用 `setInline` 把一段 SystemVerilog 代码内联进生成的 RTL。这段 SV 在时钟沿 `import "DPI-C"` 调用一个 C 函数，把当前端口信号当实参传过去。
- **`gvm_dpic.cpp`** 里同名（前缀 `c_`）的 C 函数接收这些参数，组装成结构体，`push_back` 进全局变量。
- **`gvm_global_var.cpp`** 定义这些全局变量，每周期末被 `gvm_t::clearGlobal()` 清空，形成“RTL 每拍产生 → C++ 每拍消费 → 清空”的握手。

一共 6 个钩子，覆盖 warp 全生命周期：

| 钩子 | 触发时机 | 传递的关键信息 | 全局变量 |
|------|----------|----------------|----------|
| `GvmDutCta2Warp` | CTA→warp 派发 fire | 软硬件 warp 对应关系、sgpr/vgpr 基址、wg_slot | `g_cta2warp_data` |
| `GvmDutInsnDispatch` | 指令从 ibuffer 发射 fire | pc、instr、dispatch_id、是否扩展 | `g_insn_dispatch_data` |
| `GvmDutXRegWriteback` | 标量写回 fire | rd 数据、reg_idx、hardware_warp_id | `g_xreg_wb_data` |
| `GvmDutVRegWriteback` | 向量写回 fire | 32 lane 的 rd、wvd_mask、reg_idx | `g_vreg_wb_data` |
| `GvmDutXReg` | 每个时钟沿 | 整个标量寄存器堆（按 bank 交织）的快照 | `g_xreg_data` |
| `GvmDutBarrierDone` | 屏障达成 | wg_slot_id、pc、instr | `g_bar_done_data` |

#### 4.2.2 核心流程（以 GvmDutCta2Warp 为例）

```
Scala: GvmDutCta2Warp.io.warp_req_fire  (CTA2warp.io.warpReq.fire)
        │  setInline 生成的 SV: always @(posedge clock) if(fire) c_GvmDutCta2Warp(...)
        ▼
C:    c_GvmDutCta2Warp(swg, swf, sm, hwf, sgpr_base, vgpr_base, wg_slot, nthread)
        │  组装 Cta2WarpData d; g_cta2warp_data.push_back(d);
        ▼
C++:  gvm_t::getDutWarpNew() 遍历 g_cta2warp_data，建立/更新 dut_active_warps
```

`GvmDutCta2Warp` 是整套对拍的**身份锚点**：它在 warp 刚被派发给 SM 的瞬间，把“软件身份 ↔ 硬件身份 ↔ 寄存器基址”三元关系一次性记录下来，后续所有指令和写回都靠 `sm_id+hardware_warp_id` 反查这张表。

#### 4.2.3 源码精读

**（1）`GvmDutCta2Warp`：身份锚点（Scala 侧）**

[ventus/src/GvmDutApi.scala:8-65](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/GvmDutApi.scala#L8-L65) — 端口全部 `pad(32)` 成 32 位，内联的 SV 在 `posedge clock` 且 `io_warp_req_fire` 时调用 `c_GvmDutCta2Warp(...)`，把 8 个字段传给 C++。

**（2）`GvmDutCta2Warp`：硬件连接**

[ventus/src/top/GPGPU_top.scala:500-514](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/top/GPGPU_top.scala#L500-L514) — 在 `SM_wrapper` 里例化 `gvm_cta2warp`，关键连接：

```scala
val WF_ID_WIDTH = log2Ceil(num_warp_in_a_block)
gvm_cta2warp.io.warp_req_fire       := cta2warp.io.warpReq.fire
gvm_cta2warp.io.software_wg_id      := cta2warp.io.warpReq.bits.CTAdata.dispatch2cu_wg_id.pad(32)
gvm_cta2warp.io.software_warp_id    := cta2warp.io.warpReq.bits.CTAdata.dispatch2cu_wf_tag_dispatch(WF_ID_WIDTH-1,0).pad(32)
gvm_cta2warp.io.sm_id               := sm_id.U(32.W)
gvm_cta2warp.io.hardware_warp_id    := cta2warp.io.warpReq.bits.wid.pad(32)
gvm_cta2warp.io.sgpr_base           := cta2warp.io.warpReq.bits.CTAdata.dispatch2cu_sgpr_base_dispatch.pad(32)
gvm_cta2warp.io.vgpr_base           := cta2warp.io.warpReq.bits.CTAdata.dispatch2cu_vgpr_base_dispatch.pad(32)
gvm_cta2warp.io.wg_slot_id_in_warp_sche := cta2warp.io.warpReq.bits.CTAdata.dispatch2cu_wf_tag_dispatch(TAG_WIDTH-1, WF_ID_WIDTH)
gvm_cta2warp.io.rtl_num_thread      := cta2warp.io.warpReq.bits.CTAdata.dispatch2cu_wf_size_dispatch.pad(32)
```

注意 `wf_tag` 被拆成两段（回顾 u3-l3）：低位 `WF_ID_WIDTH` 比特 = warp 在 workgroup 内的编号，作为 `software_warp_id`；高位 = `wg_slot_id_in_warp_sche`。这样 `(software_wg_id, software_warp_id)` 就成了 SPIKE 能识别的全局软件身份，而 `hardware_warp_id` 是 SM 本地的、可被复用的编号。

**（3）DPI-C 实现侧（`gvm_dpic.cpp`）**

[sim-verilator/gvm_dpic.cpp:13-31](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm_dpic.cpp#L13-L31) — `c_GvmDutCta2Warp` 把 8 个 int 参数填进 `Cta2WarpData d`，`push_back` 进 `g_cta2warp_data`。其余 5 个钩子的实现模式完全一致：

- [gvm_dpic.cpp:72-99](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm_dpic.cpp#L72-L99) — `c_GvmDutXReg` 比较特殊：它被 SV 里的 `for` 循环对每个 sgpr slot 调用一次，C++ 侧按 `xbanks_word_idx` 把字写回 `g_xreg_data[sm_id*num_bank + bank].bank_data[offset]`，逐步拼出整个交织寄存器堆快照。
- [gvm_dpic.cpp:102-121](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm_dpic.cpp#L102-L121) — `c_GvmDutVRegWriteback` 用 `(sm_id, hardware_warp_id, dispatch_id)` 三元组作 key 写入 `g_vreg_wb_data`（`std::map`），每 lane 一拍累积，从而把 32 个 lane 的写回数据归并到同一条记录。

**（4）全局变量定义**

[sim-verilator/gvm_global_var.cpp:8-13](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm_global_var.cpp#L8-L13) — 实例化各全局缓冲区；其中 `g_sgprUsage = 64` 是“每个 warp 标量寄存器用量”的临时硬编码（注释标注“临时特殊处理”）。

**（5）dispatch_id 的产生**

[sim-verilator/ibuffer.scala:153](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ibuffer.scala#L153) 与 [ibuffer.scala:161-176](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ibuffer.scala#L161-L176) — `SlowDown` 里维护一个单调递增的 `dispatchCounter`，每发射一条指令 +1（首条为 1），作为 `dispatch_id` 跟随该指令一路传到写回。注释明确：**该 id 在 SM 完成旧 warp、获得新 warp 时不会重置**，因此它在整个 SM 生命周期内全局唯一、单调，正是 retire 排序的依据。

[sim-verilator/ibuffer.scala:190-201](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/ibuffer.scala#L190-L201) — `GvmDutInsnDispatch` 把 `num_warp` 个发射通道的 `fire` 拼成位向量，SV 里逐 bit 判断哪个 warp 本拍发射了指令，逐条回调 `c_GvmDutInsnDispatch`。

**（6）写回与屏障钩子的连接**

[ventus/src/pipeline/writeback.scala:82-107](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/writeback.scala#L82-L107) — 在 `Writeback` 模块里例化 `GvmDutXRegWriteback`（标量，注意用 `RegNext` 打一拍对齐时序）和 `GvmDutVRegWriteback`（向量，直接取当拍）。[writeback.scala:41-42](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/writeback.scala#L41-L42) — `dispatch_id`、`is_extended` 等字段用 `if(GVM_ENABLED) Some(...) else None` 条件加入 Bundle，关闭 GVM 时不占面积。

[ventus/src/pipeline/warp_schedule.scala:142-153](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/warp_schedule.scala#L142-L153) — 当一个 workgroup 的所有 warp 都抵达屏障（`warp_bar_cur === warp_bar_exp`）时触发 `GvmDutBarrierDone`，传 `wg_slot_id` 而非 `hardware_warp_id`（因为屏障属于整个 workgroup）。

[ventus/src/pipeline/pipe.scala:60-72](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/pipe.scala#L60-L72) — `GvmDutXReg` 把 `operandCollector` 的 `scalarBanks` 拍平后每拍送出，作为寄存器堆快照。

#### 4.2.4 代码实践（源码阅读型）

**目标**：跟踪一条标量写回从硬件端口到 C++ 全局变量的完整路径。

1. 从 [writeback.scala:85](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/pipeline/writeback.scala#L85) 的 `gvm_x_wb.io.fire := RegNext(io.out_x.fire)` 出发。
2. 跳到 [GvmDutApi.scala:163-176](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/ventus/src/GvmDutApi.scala#L163-L176)，看 SV 如何在 `posedge` 调用 `c_GvmDutXRegWriteback`。
3. 跳到 [gvm_dpic.cpp:51-69](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm_dpic.cpp#L51-L69)，确认它把 `rd`、`reg_idx`、`hardware_warp_id`、`dispatch_id` 组装成 `XRegWritebackData` 并 `push_back`。
4. **预期观察**：理解“Scala 端口 → 内联 SV 的 DPI-C 调用 → C++ 全局 vector”三段式握手，每拍最多产生一条记录。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GvmDutXRegWriteback` 的输入用 `RegNext(...)` 打一拍，而 `GvmDutVRegWriteback` 不打？
**答案**：标量写回通路 `io.out_x` 的 `fire` 与有效数据在同一拍，但与下游采集时序存在偏移风险，用 `RegNext` 对齐到稳定值；向量写回 `io.out_v` 的数据在本拍已稳定（向量通路时序不同），故直接采样。这是时序对齐的工程处理，不是功能性差异。

**练习 2**：`dispatch_id` 为什么设计成“跨 warp 不重置”？
**答案**：retire 比对需要把 DUT 的指令按**完成顺序**与 REF 的**程序顺序**对齐。若每个 warp 各自从 0 开始计数，则不同 warp 的 dispatch_id 会撞车；而 SM 级单调递增的 id 天然给出了该 SM 内指令的全局派发次序，配合每 warp 的 `insns` map（按 dispatch_id 索引）即可重建程序顺序。

---

### 4.3 gvm_t::getDut：把原始信号组织成可比对的 DUT 视图

本模块覆盖最小模块 **gvm_t** 的“收集侧” `getDut()`。

#### 4.3.1 概念说明

6 个 DPI-C 钩子每拍往全局变量里塞的是“原始事件流”：一条条孤立的派发、写回、屏障记录，靠 `sm_id+hardware_warp_id` 标识。但 SPIKE 是按 `(software_wg_id, software_warp_id)` 索引、按程序顺序执行的。`getDut()` 的职责就是**翻译与重组**：

- 把每个新派发的 warp 登记进 `dut_active_warps`（以软件身份为 key），记录它的硬件身份、寄存器基址。
- 把每条派发的指令挂到对应 warp 的 `insns` map（以 `dispatch_id` 为 key），标记它是否“关心 retire / 关心单条比对”。
- 把每条写回/屏障完成事件，回填到对应 warp 对应指令的 `done`/`single_insn_cmp.dut_result`。
- 从交织的标量寄存器堆快照里，解交织出每个活跃 warp 的连续标量寄存器 `curr_xreg`。
- 最后清空全局变量，等待下一拍。

#### 4.3.2 核心流程

`getDut()` 是 7 个子步骤的固定流水（[gvm.cpp:74-82](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L74-L82)）：

```
getDutWarpNew()           登记新 warp（来自 g_cta2warp_data），key=(swg,swf)
getDutWarpFinish()        发现 endprg(0x0000400B) 派发 → 删除该 warp 条目
getDutInsnDispatch()      把派发的指令挂进 warp.insns[dispatch_id]
getDutInsnFinish()        用写回/屏障事件标记指令 done，记录 dut_result
  ├ getDutXRegWbFinish()    标量写回 → done + single_insn_cmp.dut_result(XREG)
  ├ getDutVRegWbFinish()    向量写回 → single_insn_cmp.dut_result(VREG)
  └ getDutBarDone()         屏障达成 → done（用 pc 识别，不用 dispatch_id）
getDutXReg()              从交织快照解交织出每个 warp 的 curr_xreg
getDutWarpNewSetRefXReg() 把新 warp 的 curr_xreg 同步给 REF（解决零初始化差异）
clearGlobal()             清空所有 g_xxx_data
```

#### 4.3.3 源码精读

**（1）登记新 warp 与身份去重**

[sim-verilator/gvm.cpp:84-121](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L84-L121) — `getDutWarpNew` 把 `g_cta2warp_data` 里每条记录转成 `dut_active_warp_t`，以 `{software_wg_id, software_warp_id}` 为 key 存入 `dut_active_warps`（`std::map`，定义见 [gvm_structs.hpp:80](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm_structs.hpp#L80)）。其中 `xreg_base = item.sgpr_base`、`xreg_usage = g_sgprUsage`（临时硬编码 64）。它会双重查重：同一软件身份或同一硬件身份重复派发都 `assert(0)`——这是对 CTA 调度器的健壮性断言。

**（2）指令派发登记与 retire 关心位**

[sim-verilator/gvm.cpp:154-195](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L154-L195) — `getDutInsnDispatch` 按 `sm_id+hardware_warp_id` 找到 warp，把指令存进 `warp.insns[dispatch_id]`，并查表设置两个关心位：

- `care` = `isInsnCare(insn, retire_care_insns)`：是否参与 retire（标量写回 + 屏障指令为 true）。
- `single_insn_cmp.care` = `isInsnCare(insn, single_insn_cmp_care_insns)`：是否参与逐条比对（向量指令为 true）。不关心的指令直接 `cmp_pass=1`。

`isInsnCare` 用“掩码+值”匹配（[gvm.cpp:22-30](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L22-L30)）：`(insn & mask) == value`。指令表见 [gvm_care_insns.cpp:87-104](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm_care_insns.cpp#L87-L104)：`retire_care_insns = XREG_INSNS + WARP_BARRIER_INSNS`，`single_insn_cmp_care_insns = VREG_INSNS`。首条指令的 `dispatch_id` 还会被记为 `base_dispatch_id` 与初始 `next_retire_dispatch_id`，作为 retire 扫描的起点。

**（3）指令完成标记**

- 标量写回：[gvm.cpp:204-257](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L204-L257) — `getDutXRegWbFinish` 断言“有标量写回的指令必然 care retire 且不是 barrier、不在 single_insn_cmp 表”，然后置 `done=true` 并记录 `dut_result.xreg_result.{rd,reg_idx}`。
- 向量写回：[gvm.cpp:259-309](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L259-L309) — `getDutVRegWbFinish` 断言“向量指令不参与 retire”，仅填充 `dut_result.vreg_result.{rd[32],reg_idx,mask}`。
- 屏障：[gvm.cpp:311-348](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L311-L348) — `getDutBarDone` 用 **pc 而非 dispatch_id** 识别指令。注释解释了原因：不同 warp 的分支行为不同，对同一条屏障指令的 `dispatch_id` 各异，而这里拿到的只是最后完成 warp 的 id；故激进假设“不会同时存在两条相同 pc 的未退休屏障”。

**（4）解交织提取标量寄存器（核心难点）**

[sim-verilator/gvm.cpp:350-371](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L350-L371) — `getDutXReg` 把交织的 bank 数据还原为每个 warp 的连续寄存器：

```cpp
for (int i = 0; i < warp.xreg_usage; ++i) {
  warp.curr_xreg[i] = g_xreg_data_mapped[sm_id]
      [(i + hardware_warp_id) % num_bank]            // 选哪个 bank
      .bank_data[(xreg_base + i) >> __builtin_ctz(num_bank)];  // bank 内偏移
}
warp.curr_xreg[0] = 0;   // 强制 x0=0
```

这正是 u4-l4 讲的交织公式的逆运算：`bank = (regIdx + wid) % num_bank`，`bank 内地址 = (base + regIdx) / num_bank`（`__builtin_ctz(num_bank)` 是 log2(num_bank)，用移位代替除法）。前置断言要求 `xreg_base` 与 `xreg_usage` 都按 `num_bank` 对齐。提取后强制 `x0=0`，假定 DUT 已正确处理 x0。

**（5）把新 warp 的寄存器同步给 REF**

[sim-verilator/gvm.cpp:373-397](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L373-L397) — `getDutWarpNewSetRefXReg` 解决一个微妙差异：SPIKE 对每个新 warp 的标量寄存器**零初始化**，而 DUT 的 CTA 调度器只分配空间**不清零**（残留旧 warp 数据）。若不处理，整堆比对会大面积误报。因此在新 warp 派发当拍，把 DUT 解交织出的 `curr_xreg` 通过 `gvmref_set_warp_xreg` 灌进 SPIKE 对应 warp，让二者起点一致。该 API 的用途在 [gvmref_interface.h:67-73](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvmref_interface.h#L67-L73) 有详细注释。

#### 4.3.4 代码实践（源码阅读型）

**目标**：手工验证解交织公式。

1. 假设 `num_bank=4`、`hardware_warp_id=1`、`xreg_base=8`、`xreg_usage=8`。
2. 对 `i=0..7`，按 [gvm.cpp:366-367](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L366-L367) 的公式计算每条 `curr_xreg[i]` 来自哪个 `(bank, bank内偏移)`。
3. **预期结果**（`__builtin_ctz(4)=2`，即除以 4）：

| i | bank=(i+1)%4 | bank内偏移=(8+i)>>2 |
|---|--------------|---------------------|
| 0 | 1 | 2 |
| 1 | 2 | 2 |
| 2 | 3 | 2 |
| 3 | 0 | 2 |
| 4 | 1 | 3 |
| 5 | 2 | 3 |
| 6 | 3 | 3 |
| 7 | 0 | 3 |

   可见连续 8 个寄存器被分散到 4 个 bank、每个 bank 取 2 个字，与 u4-l4 的交织写入一一对应。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `getDutBarDone` 用 `pc` 而不是 `dispatch_id` 找指令？
**答案**：屏障指令是 workgroup 级别的，多个 warp 各自派发同一 pc 的屏障，但它们的 `dispatch_id` 不同，而硬件回报的 `dispatch_id` 只是最后一个完成 warp 的。用 `dispatch_id` 会匹配不到其他 warp 的记录，故改用 `pc`，并假设同一时刻不存在两条相同 pc 的未退休屏障。

**练习 2**：若 `xreg_base` 不按 `num_bank` 对齐，`getDutXReg` 会怎样？
**答案**：[gvm.cpp:363](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L363) 的 `assert(warp.xreg_base % num_bank == 0)` 会触发，仿真终止。这是为了保证解交织偏移计算的整除性——CTA 调度器分配 sgpr 基址时也按 bank 数对齐（见 u3-l2 的资源分配）。

---

### 4.4 gvm_t::gvmStep：退休驱动的步进与逐条/整堆比对

本模块覆盖最小模块 **gvm_t** 的“比对侧” `gvmStep()`。

#### 4.4.1 概念说明

`getDut()` 把 DUT 状态重组好后，`gvmStep()` 负责“拉着 SPIKE 走到同样进度并比对”。难点是：DUT 是乱序的（向量结果可能先于标量完成、不同 warp 交错），而 SPIKE 必须按程序顺序逐条步进。GVM 的解法是**以 retire 为同步点**：

- 只有当 DUT 一段连续指令**按序可退休**时，才让 SPIKE 步进相同条数。
- 退休的“锚”是标量写回与屏障指令（`retire_care_insns`）；其它指令（向量、不关心的）“附着”到下一个锚上一同退休。
- 步进后做两种比对：**逐条指令比对**（`doSingleInsnCmp`，比单条指令的 rd/reg_idx/mask）与**整堆寄存器比对**（`doRetireCmp`，比整个标量寄存器堆快照）。二者互补：前者精确定位到指令，后者兜底捕捉逐条比对未覆盖的差异。

#### 4.4.2 核心流程

`gvmStep()`（[gvm.cpp:414-422](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L414-L422)）5 步：

```
checkRetire()      扫描每个 warp，找出可按序退休的连续段，写进 retire_info
stepRef()          按 retire_info 让 SPIKE 逐条步进，步进前校验 PC 一致，
                   步进后捕获 ref_result，处理 barrier 的特殊重试
doSingleInsnCmp()  对 dut_done && ref_done 的指令逐条比对（XREG/VREG）
doRetireCmp()      对每个退休段，整体比对 DUT 与 REF 的标量寄存器堆
clearInsnItem()    删除已退休且已比对完(cmp_pass!=0)的指令条目
resetRetireInfo()  清空 retire_info
```

**退休判定逻辑**（`checkRetire`，[gvm.cpp:425-492](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L425-L492)）：从 `next_retire_dispatch_id` 向后扫描——遇 `care==false`（不参与 retire的指令）计入临时计数；遇 `care==true && done==true`（已完成的标量/屏障锚）则提交临时计数、自身也计数，若是屏障则停下；遇 `care==true && done==false`（锚未完成）则停下。这样得到一个连续可退休段。随后再往后扫一遍确认没有“更后面的锚已完成”（否则说明完成顺序异常，`retiring=false` 跳过本拍）。

#### 4.4.3 源码精读

**（1）步进 REF 并校验 PC**

[sim-verilator/gvm.cpp:495-614](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L495-L614) — `stepRef` 对每个退休段，按 `retire_cnt` 逐条：

1. 若该指令 `extended`（regext 前缀），先 `gvmref_step` 一次跳过前缀（[gvm.cpp:507-509](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L507-L509)）。
2. **PC 校验**：`gvmref_get_next_pc` 取 SPIKE 即将执行的 PC，与 DUT 该指令的 `pc` 比较，不等则 `logger->error` 并 `assert`（[gvm.cpp:512-520](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L512-L520)）。这是对拍的第一道关——若连下一条指令的 PC 都对不上，说明控制流已分歧。
3. `gvmref_step` 真正步进，若 `single_insn_cmp.care` 则从返回信息提取 `ref_result`（XREG 或 VREG，[gvm.cpp:535-559](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L535-L559)）。
4. **barrier 重试**：若步进后 SPIKE 的 PC 没前进且当前指令是屏障，置 `barrier_retry=true`（[gvm.cpp:528-532](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L528-L532)），本条不标 `retired`，留到 `stepRef` 末尾的第二个循环单独再步进一次（[gvm.cpp:577-613](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L577-L613)）。这处理了 SPIKE 中屏障在所有 warp 到齐前 PC 不前进的语义。

**（2）逐条指令比对**

[sim-verilator/gvm.cpp:616-724](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L616-L724) — `doSingleInsnCmp` 遍历所有 warp 的所有指令，对 `dut_done && ref_done` 且 `care` 的指令：

- **XREG**：比 `rd` 与 `reg_idx`，不等则 `cmp_pass=-1` 并打印 `DUT reg_idx / REF reg_idx / DUT rd / REF rd`。
- **VREG**：先比 `reg_idx` 与 32 位 `mask`；相同再逐 lane 比 `rd[i]`。若是浮点指令（`fp32_vreg_insns`），用容差比较 `|dut-ref| > atol + rtol*|ref|`（默认 `fp32_atol=fp32_rtol=1e-3`，[gvm.hpp:46-47](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.hpp#L46-L47)）；否则严格相等。被 mask 关闭的 lane（`mask[i]==false`）跳过比较。

错误信息会打印完整的 `sm_id / hardware_warp_id / software_wg_id / software_warp_id / dispatch_id / pc / insn`，这就是定位 RTL bug 的关键线索。

**（3）整堆寄存器比对（兜底）**

[sim-verilator/gvm.cpp:726-743](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L726-L743) — `doRetireCmp` 对每个退休段，用 `gvmref_get_xreg` 取 SPIKE 的全部 256 个标量寄存器，逐个与 DUT 的 `curr_xreg[i]`（由 `getDutXReg` 每拍刷新）比较，不等则报 `reg x{i}: DUT=0x.. REF=0x..`。即使某条标量指令不在 `single_insn_cmp` 表里、逐条比对漏掉，整堆比对也能兜住。

**（4）指令条目回收**

[sim-verilator/gvm.cpp:745-757](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L745-L757) — `clearInsnItem` 只删除队头那些 `retired==true && cmp_pass!=0`（已比对完，无论通过与否）的条目；遇到未决条目就 `break`，保证 map 的 dispatch_id 顺序不被打乱。

#### 4.4.4 代码实践

**目标**：启用 GVM 构建并运行测试用例，观察对拍输出（**运行部分待本地验证**，因需外部 `libgvmref.so`）。

1. 准备 SPIKE 参考模型动态库 `libgvmref.so`，放入 `gvm.mk` 的 `GVM_REF_DIR`（默认 `../../install/lib`）。
2. 在 `sim-verilator/` 下用 GVM 构建脚本编译（`gvm.mk` 已设 `RTL_GVM_ENABLED=true` 与 `-DENABLE_GVM=1`），产出 `libVentusGVM.so`。
3. 用配套的 GVM driver 加载一个测试用例（如 `vecadd`）运行。
4. **观察现象**：
   - 正常时日志（spdlog debug 级）会打印每段 `GVM retire: sm_id ... dispatch_id ... pc ... insn ... <指令名>`。
   - 出错时会打印 `GVM error: DUT and REF ... mismatch at ...` 行，包含精确的 warp 身份与 pc/insn。
5. **定位一处不一致**：在输出中搜索 `GVM error`，取第一行的 `sm_id / hardware_warp_id / software_wg_id / software_warp_id / dispatch_id / pc / insn`，结合波形反查该 SM 该 warp 在该 PC 的执行。
6. **说明 getDut 的作用**：定位所依赖的 `(software_wg_id, software_warp_id)` 正是 `getDutWarpNew`（[gvm.cpp:84-121](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L84-L121)）在 warp 派发时由 `GvmDutCta2Warp` 建立的；而要比对的标量寄存器基址（`xreg_base=sgpr_base`）与解交织公式（[gvm.cpp:366-367](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L366-L367)）把 DUT 的交织存储映射回软件视角的连续寄存器，才能与 SPIKE 同 `reg_idx` 对齐比较。

> 若本地暂无 `libgvmref.so`，可降级为源码阅读实践：在 `gvm.cpp` 中把 `doSingleInsnCmp` 的 `logger->error(...)`（[gvm.cpp:632](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm.cpp#L632)）当作“不一致报告行”的模板，逐字段说明每个字段来自哪个钩子。

#### 4.4.5 小练习与答案

**练习 1**：为什么需要 `doRetireCmp` 整堆比对，明明 `doSingleInsnCmp` 已经逐条比对了标量写回？
**答案**：`doSingleInsnCmp` 只比对**进入 `single_insn_cmp_care_insns`/`retire_care_insns` 表**的指令；表是有限的（见 `gvm_care_insns.cpp`），未列入的标量指令不会被逐条比对。整堆比对以“快照 diff”方式兜底，能发现任何最终落到标量寄存器堆里的差异，无论该指令是否在表内。

**练习 2**：浮点向量的容差比较公式是什么？为什么需要容差？
**答案**：判据为 `|dut - ref| > fp32_atol + fp32_rtol * |ref|`（默认各 1e-3）才报错。因为 DUT 的 FPU（fpuv2）与 SPIKE 的浮点实现可能在舍入、FMA 融合等细节上有 1 ULP 级差异，严格逐位相等会大量误报，故用相对+绝对容差。

**练习 3**：`checkRetire` 在得到候选退休段后，为什么还要再往后扫一遍确认 `retiring`？
**答案**：若候选段**之后**存在一个 `care==true && done==true` 的锚，说明 DUT 出现了“后面的标量指令先于前面完成”的乱序，这违背了 retire 比对所需的按序完成前提；此时置 `retiring=false` 跳过本拍，等前面指令也完成后再退休，避免把乱序完成误当成按序退休喂给 SPIKE。

---

## 5. 综合实践

**任务：画一张 GVM 单周期数据流图，并标注一次“标量写回不一致”的完整定位链路。**

要求：

1. 在同一张图上画出：DUT（Verilog）→ `GvmDut*` 钩子 → `g_xxx_data` 全局变量 → `gvm_t::getDut()` → `dut_active_warps` → `gvm_t::gvmStep()` → `gvmref_*` → SPIKE，标注每一段对应的源码文件与行号。
2. 假设仿真报告了这样一行：
   `GVM error: DUT and REF insn result mismatch ... insn_type XREG, DUT reg_idx: 7, REF reg_idx: 7, DUT rd: 0x00000009, REF rd: 0x00000008`。
3. 写出定位这处 bug 时，你会按什么顺序查阅：先看 `dispatch_id/pc/insn` → 在 `getDutInsnDispatch` 找该指令登记 → 在 `getDutXRegWbFinish` 看写回来源 → 在 `doSingleInsnCmp` 看比对逻辑 → 结合波形看 DUT 该执行单元的输入操作数。
4. 说明 `getDut` 在其中起的作用：是它把“硬件身份 `sm_id+hardware_warp_id`”翻译成“软件身份 `(swg,swf)`”、把交织寄存器解交织，才让这行错误信息同时携带 `hardware_warp_id`（用于查波形）和 `software_wg_id/software_warp_id`（用于查 SPIKE 状态）。

**预期产出**：一张清晰的数据流图 + 一段定位步骤说明。本任务为源码阅读与文档型，无需运行仿真。

## 6. 本讲小结

- GVM 是 RTL 与 SPIKE 参考模型的**逐指令对拍引擎**，每个时钟周期在 `step()` 末尾运行一次 `getDut()` + `gvmStep()`，能在程序最终结果还正确时就暴露单条指令的隐藏错误。
- `RTL_GVM_ENABLED`（Chisel 层，决定生成 `GvmDut*` 钩子）与 `ENABLE_GVM`（C++ 层，决定编译 `gvm_t` 与链接 `libgvmref`）是两个必须**同时开启**的开关；普通仿真默认都关，GVM 专用构建（`gvm.mk`）才都开。
- `GvmDutApi.scala` 用 `ExtModule + setInline` 生成 6 个 DPI-C 探测模块，`gvm_dpic.cpp` 把信号塞进全局变量；其中 **`GvmDutCta2Warp`** 是身份锚点，在 warp 派发瞬间记录软硬件身份与 sgpr/vgpr 基址的对应关系。
- `getDut()` 把零散事件重组为按 `(software_wg_id, software_warp_id)` 索引、按 `dispatch_id` 排序的 `dut_active_warps`，并从交织的标量寄存器堆快照中**解交织**出每个 warp 的连续 `curr_xreg`，还把新 warp 的初值同步给 SPIKE 以消除“DUT 不清零”差异。
- `gvmStep()` 以**标量写回与屏障指令为 retire 锚**同步步进 SPIKE，步进前校验 PC，步进后做“逐条指令比对（XREG/VREG，浮点带容差）”与“整堆标量寄存器快照比对”双重检查，任何不一致都打印携带完整身份与 pc/insn 的错误行。

## 7. 下一步学习建议

- **扩展指令覆盖**：阅读 [gvm_care_insns.cpp](https://github.com/THU-DSP-LAB/ventus-gpgpu/blob/681172541a8a34ffb43c483a19c075acbc11a4eb/sim-verilator/gvm_care_insns.cpp) 与脚本 `gvm-extract-vreginsns.py`，理解向量指令表是如何从编译器/反汇编自动抽取的，尝试为一条新指令添加比对支持。
- **深入参考模型侧**：本仓库只提供 `gvmref_interface.h` 这个 API 契约，`libgvmref.so`（改造版 SPIKE）在配套仓库。可结合 SPIKE 的 retire 回调机制理解 `gvmref_step` 如何返回 `insn_result`。
- **回顾闭环**：重读 u3-l3（CTA2warp 的 wid 分配与 wf_tag）、u4-l4（寄存器堆交织）、u5-l6（写回仲裁），体会 GVM 的身份翻译与解交织为何正好对应这些硬件设计。
- **下一篇**：u7-l5（FPGA 部署与参数定制）将离开仿真、走向综合上板，GVM 钩子在那条路上会被 `GVM_ENABLED=false` 关闭。

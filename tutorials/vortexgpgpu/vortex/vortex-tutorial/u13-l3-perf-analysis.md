# 性能计数器与 roofline 分析

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Vortex 性能计数器的「两段式使能」机制：编译期 `PERF_ENABLE` 控制硬件是否插桩，运行期 `VORTEX_PROFILING` 选择打印哪一类计数器。
- 读懂 `perf.cpp` 打印出的四组核心指标——调度器利用率、流水线停顿、指令混合、内存延迟——并能解释它们各自由哪些裸计数器算出来。
- 会用 `perf/roofline.py` 在 `blackbox.sh` 之上做微架构旋钮搜索，并画出/读懂一张 roofline 图（峰值算力、峰值带宽、脊点 ridge、算术强度 AI）。
- 理解 `perf_gate` 这道 CI 性能回归门控为何「基线不可手改」，以及 ±2% 容差里上界（回归门）与下界（ratchet 棘轮）各自的含义。
- 把 Perfetto 的逐周期 warp 状态视图当作 `perf.cpp` 聚合百分比指标的「时序维度补充」来使用。

## 2. 前置知识

- **MPM（Memory/Performance Monitor）计数器**：Vortex 在硬件里维护的一组 64 位事件计数器，每个核、每个缓存级、每个加速器各有一套。它们映射成一段连续的 RISC-V CSR（`VX_CSR_MPM_BASE` 起），主机可以经命令处理器（CP）读回。可以把它理解成 GPU 上的性能监视单元（PMU）。
- **类（class）**：计数器太多，Vortex 把它们分成互斥的若干类（core / icache / dcache / l2 / l3 / mem / tcu / raster / tex / om / rtu / dxa），同一段 CSR 地址在不同类下表示不同计数器。一次运行只能选中一类来读。
- **IPC / cycles / instrs**：和 [u7-l4](u7-l4-model-parity.md) 一致，`instrs` 是各核 `MINSTRET` 之和，`cycles` 是各核 `MCYCLE` 的最大值，`IPC = instrs / cycles`。这一讲所有指标都以这三个量为分母。
- **roofline 模型**：一种把程序性能拆成「算力受限」或「带宽受限」的可视化方法，横轴是算术强度（每字节做多少次运算，FLOP/byte），纵轴是性能（FLOP/cycle 或 GFLOP/s），屋顶由峰值算力（水平线）和峰值带宽（斜线）取最小值构成。
- **blackbox.sh / CONFIGS**：见 [u1-l4](u1-l4-first-run.md)。`--perf=N` 是它的一个旋钮，本讲会拆开看它到底做了哪两件事。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [sw/runtime/common/perf.cpp](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp) | 主机侧报告生成器：读 MPM 计数器、算成百分比/比率、打印 `PERF:` 行 |
| [ci/blackbox.sh](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh) | `--perf=N` 旋钮的两段式实现：注入 `-DPERF_ENABLE` 并导出 `VORTEX_PROFILING` |
| [VX_types.toml](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml) | `VX_DCR_MPM_CLASS_*` 枚举，定义类编号（1=core, 3=icache, …） |
| [hw/rtl/core/VX_scheduler.sv](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv) | RTL 侧 `ifdef PERF_ENABLE` 计数器插桩的典型例子 |
| [perf/roofline.py](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/perf/roofline.py) | 旋钮搜索器 + roofline 绘图器，驱动 blackbox 跑程序并解析 `PERF:` 行 |
| [ci/test_runner.py](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py) | `_perf_gate`：把 rtlsim 的 cycles 和黄金基线比较的性能回归门控 |
| [ci/perf_baseline.py](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/perf_baseline.py) | 基线读写与 ±2% 容差常量 |
| [ci/perf/baselines/perf_gate.json](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/perf/baselines/perf_gate.json) | 黄金基线数据（cycles/instrs/config_hash） |
| [docs/perfetto_analysis.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/perfetto_analysis.md) | 把运行 trace 转成 Perfetto 可视化、用 warp 状态计数器做时序分析 |
| [AGENTS.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md) | §4 中的 perf_gate / model_parity 纪律 |

---

## 4. 核心概念与源码讲解

### 4.1 性能计数器的两段式使能与 MPM 类系统

#### 4.1.1 概念说明

Vortex 的性能计数器**默认是关着的**，因为插桩会改变 RTL/SimX 的实现面积与编译产物。打开它需要两个独立动作，缺一不可：

1. **编译期插桩**：加宏 `PERF_ENABLE`。这是一个硬开关——只有定义了它，RTL/SimX 模块里那一大段 `` `ifdef PERF_ENABLE `` 的计数器逻辑才会被综合/编译进来。没定义时，计数器寄存器根本不存在。
2. **运行期选类**：设环境变量 `VORTEX_PROFILING=<class>`。它决定主机侧报告函数**打印哪一类**计数器（core？dcache？mem？）。

把这两步合一的是 `blackbox.sh` 的 `--perf=N`：它同时完成「注入 `-DPERF_ENABLE`」和「把 `N` 记成 `PERF_CLASS`、稍后导出为 `VORTEX_PROFILING`」。这里的 `N` 不是随便的数字，而是 `VX_DCR_MPM_CLASS_*` 枚举值。

#### 4.1.2 核心流程

```text
blackbox.sh --perf=N
   │
   ├── CONFIGS += "-DPERF_ENABLE"        # 编译期：RTL/SimX 里 ifdef PERF_ENABLE 的计数器被启用
   └── PERF_CLASS=N
         └── export VORTEX_PROFILING=N   # 运行期：报告函数据此选择 MPM 类
                        │
                        ▼
   vx_device_dump_perf(dev)              # 程序末尾调用（如 sgemm main.cpp 末行）
        │ 读 VORTEX_PROFILING → mpm_class
        │ switch(mpm_class) { case CORE: ...; case DCACHE: ... }
        │ 每个指标：vx_device_mpm_query() 经 CP 读 64 位计数器
        └── 打印 "PERF: ..." 行
```

`N` 到类名的对照来自 `VX_types.toml`：`1=core, 3=icache, 4=dcache, 5=l2cache, 6=l3cache, 7=mem, 11=tcu, 12=raster, 13=tex, 14=om, 15=rtu, 16=dxa`（`2/8/9/10` 是保留值）。

#### 4.1.3 源码精读

`blackbox.sh` 中 `--perf` 的处理——一行干两件事：

```bash
--perf=*)   CONFIGS=$(add_option "$CONFIGS" "-DPERF_ENABLE"); PERF_CLASS=${i#*=} ;;
```

[ci/blackbox.sh:73](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L73)：既把 `-DPERF_ENABLE` 累加进 `CONFIGS`（编译期插桩），又把 `N` 存进 `PERF_CLASS`。

```bash
export VORTEX_PROFILING=$PERF_CLASS
```

[ci/blackbox.sh:180](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L180)：运行前把类号导出成 `VORTEX_PROFILING`，供主机程序读。

主机侧用一个单例读它：

```cpp
ProfilingMode() : mpm_class_(0) {
  auto profiling_s = getenv("VORTEX_PROFILING");
  if (profiling_s) {
    mpm_class_ = std::atoi(profiling_s);
  }
}
```

[sw/runtime/common/perf.cpp:41-48](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L41-L48)：构造时读 `VORTEX_PROFILING`，没设就默认 `0`（`BASE` 类，除最后的 IPC 行外什么都不打印）。

类枚举本身定义在共享 ABI 契约里：

```toml
VX_DCR_MPM_CLASS_BASE    = 0
VX_DCR_MPM_CLASS_CORE    = 1
...
VX_DCR_MPM_CLASS_MEM     = 7
VX_DCR_MPM_CLASS_TCU     = 11
VX_DCR_MPM_CLASS_DXA     = 16
```

[VX_types.toml:565-581](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/VX_types.toml#L565-L581)：`blackbox.sh --perf` 帮助文本里的数字就来自这张表（见 [ci/blackbox.sh:31-32](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/blackbox.sh#L31-L32)）。

RTL 侧的插桩长什么样？调度器是最典型的例子：

```systemverilog
`ifdef PERF_ENABLE
    reg [PERF_CTR_BITS-1:0] perf_sched_idles;
    ...
    always @(posedge clk) begin
      if (reset) ...
      else begin
        perf_sched_idles   <= perf_sched_idles + PERF_CTR_BITS'(schedule_idle);
        perf_active_warps  <= perf_active_warps + PERF_CTR_BITS'(active_warps_cnt);
        perf_stalled_warps <= perf_stalled_warps + PERF_CTR_BITS'(stalled_warps_cnt);
        perf_issued_warps  <= perf_issued_warps  + PERF_CTR_BITS'(schedule_if_fire);
        perf_issued_threads<= perf_issued_threads+ PERF_CTR_BITS'(issued_threads_cnt);
        ...
      end
    end
```

[hw/rtl/core/VX_scheduler.sv:629-668](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/rtl/core/VX_scheduler.sv#L629-L668)：整段被 `` `ifdef PERF_ENABLE `` 包住，没定义时这些寄存器不综合。注意 `schedule_idle = ~schedule_valid`——这一拍调度器没选出任何 warp 就计数一次空闲，这正是后面 `idle%` 的来源。

读计数器走的是 CP（命令处理器），因为公开的 DCR 读接口带不上 MPM 的类标签：

```cpp
const uint32_t csr_id  = addr - VX_CSR_MPM_BASE;
const uint32_t clss_sh = mpm_class << (16 + 6);
auto read_one = [&](uint32_t cid, uint64_t* out) -> vx_result_t {
  uint32_t lo = 0, hi = 0;
  uint32_t tag_lo = clss_sh | ((csr_id << 16) | cid);
  uint32_t tag_hi = clss_sh | (((csr_id + 32) << 16) | cid);
  auto r = device->cp_submit_dcr_read(VX_DCR_BASE_MPM_VALUE, tag_lo, &lo);
  ...
  r = device->cp_submit_dcr_read(VX_DCR_BASE_MPM_VALUE, tag_hi, &hi);
  *out = ((uint64_t)hi << 32) | lo;
  return VX_SUCCESS;
};
```

[sw/runtime/common/perf.cpp:196-235](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L196-L235)：一个 64 位计数器被拆成两次 32 位 DCR 读——低半在 `csr_id`、高半在 `csr_id+32`，每次都把 `mpm_class|csr_id|core_id` 打包进 tag。`core_id == 0xffffffff` 表示「跨所有核求和」。

#### 4.1.4 代码实践

1. **目标**：直观看到两段式使能的效果。
2. **步骤**（从 `build/` 目录）：
   - 跑 `./ci/blackbox.sh --driver=simx --app=demo`（不带 `--perf`），观察末尾输出——只有一行形如 `PERF: instrs=..., cycles=..., IPC=...`。
   - 再跑 `./ci/blackbox.sh --driver=simx --app=demo --perf=1`，观察多出来的一大段 `PERF: scheduler: ...`、`PERF: stalls: ...`、`PERF: inst_mix: ...` 等行。
   - 把 `--perf=1` 换成 `--perf=4`（dcache），观察输出变成 `PERF: coreN: dcache: ...` 行。
3. **需要观察的现象**：不带 `--perf` 时没有详细计数器段；`--perf=1` 给 core 类、`--perf=4` 给 dcache 类，二者互斥。
4. **预期结果**：详细段的「有无」由 `PERF_ENABLE`（编译期）决定，详细段的「内容」由 `VORTEX_PROFILING`（运行期）决定。
5. 运行结果：待本地验证（取决于你的 build 树是否已 configure）。

#### 4.1.5 小练习与答案

- **练习 1**：如果只设 `export VORTEX_PROFILING=1` 但不加 `--perf`（即没注入 `PERF_ENABLE`），会看到 core 类报告吗？
  - **答**：不会。`VORTEX_PROFILING=1` 只是让 `vx_device_dump_perf` 走 `CORE` 分支并去读那些 CSR，但 RTL/SimX 里计数器根本没被综合进来，读到的值是 0（或无效），报告会打印但全是零。两个动作缺一不可。
- **练习 2**：`--perf=7` 对应哪一类？为什么 `--perf=2` 没用？
  - **答**：`7` 是 `VX_DCR_MPM_CLASS_MEM`（本地内存/coalescer/VM/全局 DRAM）。`2` 是 `VX_DCR_MPM_CLASS_RESERVED1`，保留值，`perf.cpp` 的 switch 落到 `default` 会报 `invalid profiling class`。

---

### 4.2 perf.cpp 报告生成器：从裸计数器到性能指标

#### 4.2.1 概念说明

`vx_device_dump_perf` 是主机程序在退出前调用的报告函数（例如 `tests/regression/sgemm/main.cpp` 末尾的 `vx_device_dump_perf(dev, stdout)`）。它的职责是把一组**裸的 64 位事件计数**翻译成人能读的**百分比与比率**。

CORE 类报告是本讲的重点，它有四组指标，全部存在 `CoreCounters` 结构里：

| 组 | 字段（节选） | 含义 |
|----|------------|------|
| 调度器 | `sched_idle` / `active_warps` / `stalled_warps` / `issued_warps` / `issued_threads` | 空闲拍、活跃 warp、停顿 warp、发射 warp、发射线程的累计量 |
| 流水线停顿 | `stall_fetch/ibuf/scrb/opds/alu/fpu/lsu/sfu/tcu` | 各级各功能单元的停顿拍数 |
| 指令混合 | `instr_alu/fpu/lsu/sfu/tcu` | 各类指令退休计数 |
| 分支 | `branches` / `divergence` | 总分支数、其中发散分支数 |
| 内存 | `ifetches/ifetch_lt/loads/load_lt/stores` | 取指/访存次数与累计延迟 |

#### 4.2.2 核心流程

报告把裸计数器除以 `cycles`（或 `cycles × issue_width`）得到百分比。关键公式（用 `safe_div` 防除零）：

- 调度器空闲率：\( \text{idle\%} = \frac{\text{sched\_idle}}{\text{cycles}} \times 100\% \)
- 平均占用（warp/拍）：\( \overline{\text{occ}} = \frac{\text{active\_warps}}{\text{cycles}} \)，归一化 \( \text{occ\%} = \frac{\overline{\text{occ}}}{\text{NUM\_WARPS}} \times 100\% \)
- SIMT 利用率（线程/warp）：\( \text{warp\_eff} = \frac{\text{issued\_threads}}{\text{issued\_warps}} \)，归一化到 `NUM_THREADS`
- 停顿率：前端两级（fetch/ibuf）除以 `cycles`；后端（scrb/opds/alu/lsu/sfu/fpu/tcu）除以 `cycles_wide = cycles × issue_width`（因为后端按发射宽度计拍）。
- 命中率（`calc_ratio`）：\( \text{hit\%} = \left(1 - \frac{\text{miss}}{\text{accesses}}\right) \times 100\% \)
- 利用率（`calc_utility`）：\( \text{utility\%} = \frac{\text{useful}}{\text{useful} + \text{stalls}} \times 100\% \)

全核汇总用「`instrs` 求和、`cycles` 取最大」，与 u7-l4 完全一致，保证 IPC 口径统一。

#### 4.2.3 源码精读

裸计数器集合就是 `CoreCounters`：

```cpp
struct CoreCounters {
  uint64_t cycles = 0;
  uint64_t instrs = 0;
  // scheduler
  uint64_t sched_idle = 0;
  uint64_t active_warps = 0;
  uint64_t stalled_warps = 0;
  uint64_t issued_warps = 0;
  uint64_t issued_threads = 0;
  // pipeline stalls
  uint64_t stall_fetch = 0;
  ...
  uint64_t stall_lsu = 0;
  // workload mix
  uint64_t instr_lsu = 0;
  ...
  // memory (front-end + LSU)
  uint64_t loads = 0;
  uint64_t load_lt = 0;
  ...
};
```

[sw/runtime/common/perf.cpp:110-153](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L110-L153)：注意 `load_lt` 是「所有 load 的累计延迟」而非平均——后面要除以 `loads` 才得到 `load_lat`。

四个核心 helper：

```cpp
static inline int calc_percent(uint64_t part, uint64_t total) {
  return (total == 0) ? 0 : (int)std::lround(safe_div((double)part, (double)total) * 100.0);
}
static inline int calc_ratio(uint64_t misses, uint64_t accesses) {
  ...
  double miss_rate = safe_div((double)misses, (double)accesses);
  return (int)std::lround((1.0 - miss_rate) * 100.0);
}
static inline int calc_utility(uint64_t useful, uint64_t stalls) {
  return calc_percent(useful, useful + stalls);
}
```

[sw/runtime/common/perf.cpp:69-82](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L69-L82)：`calc_ratio` 输入的是 miss 数却返回 hit 率（`1 - miss_rate`），`calc_utility` 把「有用工作量」和「停顿」放在一起算占比。

调度器报告是四组里信息量最大的：

```cpp
const int idle_pct = calc_percent(c.sched_idle, cycles);
const double avg_occ = safe_div((double)c.active_warps, (double)cycles);
const int occ_pct = (int)std::lround(safe_div(avg_occ, (double)num_warps) * 100.0);
const double warp_eff = safe_div((double)c.issued_threads, (double)c.issued_warps);
const int warp_eff_pct = (int)std::lround(safe_div(warp_eff, (double)num_threads) * 100.0);
perf_print_core(stream, core_id, "scheduler: idle=%d%%, occupancy=%.1f (%d%%), simt_util=%.1f (%d%%)",
                idle_pct, avg_occ, occ_pct, warp_eff, warp_eff_pct);
```

[sw/runtime/common/perf.cpp:345-351](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L345-L351)：三个利用率——`idle%`（调度器空转比例）、`occupancy`（平均每拍活跃 warp 数 / 总 warp 数）、`simt_util`（每发射 warp 平均有效线程数 / 每warp线程数）。后两者分别度量「有没有足够并发」和「有没有分支发散」。

停顿报告按级罗列，注意分母不同：

```cpp
std::vector<Metric> stall_metrics = {
  {"fetch", calc_percent(c.stall_fetch, cycles), true},
  {"ibuf",  calc_percent(c.stall_ibuf, cycles), true},
  {"scrb",  calc_percent(c.stall_scrb, cycles_wide), true},
  {"opds",  calc_percent(c.stall_opds, cycles_wide), true},
  {"alu",   calc_percent(c.stall_alu, cycles_wide), true},
  {"lsu",   calc_percent(c.stall_lsu, cycles_wide), true},
  ...
};
```

[sw/runtime/common/perf.cpp:354-365](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L354-L365)：`fetch/ibuf` 在调度器之后、按 warp 拍计，分母是 `cycles`；`scrb` 之后各级处于发射宽度为 `ISSUE_WIDTH` 的并行段，分母用 `cycles_wide = cycles * issue_width`（见 [perf.cpp:341](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L341)）。`fpu/tcu` 两项还受 `fpu_en/tcu_en` 门控，未启用不打印。

内存延迟报告把累计延迟除成平均值：

```cpp
const double ifetch_avg_lt = safe_div((double)c.ifetch_lt, (double)c.ifetches);
const double load_avg_lt = safe_div((double)c.load_lt, (double)c.loads);
perf_print_core(stream, core_id, "memory: ifetch_lat=%.2f, load_lat=%.2f, loads=..., stores=...",
                ifetch_avg_lt, load_avg_lt, c.loads, c.stores);
```

[sw/runtime/common/perf.cpp:383-386](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L383-L386)：`load_lat` 是「每次 load 平均花了多少拍」——它直接反映缓存命中率与内存压力。最后一条永远是全局 IPC：

```cpp
double global_ipc = safe_div((double)total_instrs, (double)max_cycles);
perf_print(stream, "instrs=%" PRIu64 ", cycles=%" PRIu64 ", IPC=%.3f", total_instrs, max_cycles, global_ipc);
```

[sw/runtime/common/perf.cpp:776-777](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L776-L777)：`total_instrs` 是各核 `MINSTRET` 之和，`max_cycles` 是各核 `MCYCLE` 的最大值——这正是 roofline.py 解析的那一行。

#### 4.2.4 代码实践

1. **目标**：学会用 CORE 报告定位瓶颈是「算力」「停顿」还是「内存」。
2. **步骤**（从 `build/` 目录）：
   - 跑 `./ci/blackbox.sh --driver=simx --app=sgemm --args="-n128" --perf=1`。
   - 在输出里找到 `PERF: scheduler:`、`PERF: stalls:`、`PERF: inst_mix:`、`PERF: memory:` 四行，以及结尾的 `PERF: instrs=..., IPC=...`。
3. **需要观察的现象**：
   - `stalls:` 行里哪一项占比最高？对 sgemm 这种计算密集型，通常 `lsu` 或 `scrb` 较高，`alu/fpu` 反映功能单元冲突。
   - `inst_mix:` 行里 `fpu`/`alu` 的比例，验证它确实是个浮点密集 kernel。
   - `memory:` 行的 `load_lat`——延迟越大说明缓存未命中越多。
4. **预期结果**：能把 IPC 偏低归因到某一个停顿源（例如「`lsu` 停顿高 + `load_lat` 大」= 带宽/命中率受限）。
5. 运行结果：待本地验证（具体百分比取决于你的 `VX_config.toml` 基线）。

#### 4.2.5 小练习与答案

- **练习 1**：`occupancy=2.3 (29%)` 在 `NUM_WARPS=8` 下表示什么？如果它是 `0.3 (4%)` 又说明什么？
  - **答**：每拍平均有 2.3 个 warp 处于 active 状态，占 8 个 warp 的 29%。若是 `0.3` 则调度器严重「喂不饱」，多数拍里几乎没有可运行 warp——常见于 kernel 末尾或工作量太小。
- **练习 2**：为什么 `stalls` 里 `fetch/ibuf` 用 `cycles` 做分母，而 `scrb/opds/alu` 用 `cycles_wide`？
  - **答**：`fetch/ibuf` 处于调度器选出 warp 之后、按每 warp 一拍推进的串行段，拍数等于 `cycles`；从 `scrb` 起进入宽度为 `ISSUE_WIDTH` 的并行发射段，每拍有 `issue_width` 个发射槽，所以「总槽位数」是 `cycles × issue_width`，停顿要相对这个量归一。
- **练习 3**：`calc_utility(reqs, bank_st)` 对缓存返回的「utility%」含义是什么？
  - **答**：\( \frac{\text{reqs}}{\text{reqs} + \text{bank\_st}} \)，即「非 bank 停顿的拍占比」。utility 低意味着请求被 bank 冲突大量卡住。

---

### 4.3 roofline.py：微架构旋钮搜索与 roofline 可视化

#### 4.3.1 概念说明

`perf/roofline.py` 是架在 `blackbox.sh` 之上的两层工具：

1. **配置搜索器**：把一组微架构旋钮（线程数、warp 数、issue 宽度、FPU 块数、各级 cache 的 size/ways/banks/MSHR……）当成搜索空间，反复跑同一个程序、读回 IPC，找出 IPC 最高的配置。
2. **roofline 绘图器**：对（最优）配置画一张 roofline 图，把程序的算术强度、峰值算力、峰值带宽、脊点画在一起，判断它受限于算力还是带宽。

roofline 的核心数学（周期域，`--freq=0`）：

\[
\text{PeakCompute} = 2 \cdot \text{cores} \cdot \text{threads} \cdot \text{fpu\_blocks} \quad [\text{FLOP/cycle}]
\]

\[
\text{PeakBW} = \text{mem\_data\_size} \cdot \text{mem\_banks} \quad [\text{B/cycle}]
\]

\[
\text{Roofline}(AI) = \min(\text{PeakBW} \cdot AI,\ \text{PeakCompute})
\]

\[
\text{Ridge} = \frac{\text{PeakCompute}}{\text{PeakBW}} \quad [\text{FLOP/B}]
\]

其中算术强度 \( AI = \frac{\text{FLOPs}}{\text{bytes}} \)。程序的运行点落在屋顶下方某处：若 \( AI < \text{Ridge} \) 则带宽受限（在斜线上），若 \( AI > \text{Ridge} \) 则算力受限（在水平线下）。

#### 4.3.2 核心流程

```text
解析 --threads=4,8,16 等旋钮 → 构建搜索空间 space{...}
   │
   ├─ strategy = single / random / fast / best
   │     │
   │     └─ 对每个候选配置 cfg:
   │           CONFIGS = "-DVX_CFG_NUM_THREADS=4 ..." (build_configs_env)
   │           blackbox.sh --driver --app --args --perf=1  (env CONFIGS=...)
   │           用正则抓 "PERF: instrs=.., cycles=.., IPC=.."  → (ipc, instrs, cycles)
   │           抓 "PERF: memory: ... read_bytes=.. write_bytes=.." → total_bytes
   │
   ├─ 缓存：cache_key(cfg) 命中则跳过（smoke.cache, JSONL 追加）
   │
   └─ 选出 IPC 最高的 cfg → plot_roofline() → 保存 PNG
```

四种策略：`single`（只跑一组）、`random`（随机采样 N 组）、`fast`（坐标下降：按 KNOBS 优先级逐个旋钮扫，一轮无改进即收敛）、`best`（穷举合法空间）。旋钮之间有约束（2 的幂、`issue_width ≤ warps`、`*_blocks ≤ issue_width`、cache size 与 ways 对齐），由 `validate()` 把关。

#### 4.3.3 源码精读

旋钮表是搜索的骨架，顺序即 `fast` 策略的扫描优先级：

```python
KNOBS = [
    ("threads",     "NUM_THREADS",    "--threads",     True,  False),
    ("warps",       "NUM_WARPS",      "--warps",       True,  False),
    ("issue_width", "ISSUE_WIDTH",    "--issue-width", True,  False),
    ("fpu_blocks",  "NUM_FPU_BLOCKS", "--fpu-blocks",  False, False),
    ...
    ("dcache_mshr", "DCACHE_MSHR_SIZE","--dcache-mshr", False, False),
    ...
    ("l2_enable",   "L2_ENABLE",      "--l2-enable",   False, True),
    ...
    ("mem_banks",   "PLATFORM_MEMORY_NUM_BANKS",  "--mem-banks",  True, False),
    ("mem_data_size","PLATFORM_MEMORY_DATA_SIZE", "--mem-data-size",True,False),
]
```

[perf/roofline.py:59-91](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/perf/roofline.py#L59-L91)：每项是 `(名字, VX_CFG_宏, blackbox flag, 是否必须2的幂, 是否布尔)`。`build_configs_env()` 据此把 cfg 翻译成 `-DVX_CFG_NUM_THREADS=4 ...` 串塞进 `CONFIGS`。

解析靠正则——这正则决定了 roofline.py 必须配 `--perf=1`：

```python
m = re.findall(r"PERF:\s+instrs=(\d+),\s*cycles=(\d+),\s*IPC=([0-9.]+)", proc.stdout)
if not m:
    return None, None, None, None, output + "\nERROR: no PERF summary line"
instrs, cycles, ipc = int(m[-1][0]), int(m[-1][1]), float(m[-1][2])

mem = re.search(r"PERF:\s+memory:.*?read_bytes=(\d+).*?write_bytes=(\d+)", proc.stdout)
total_bytes = int(mem.group(1)) + int(mem.group(2)) if mem else None
```

[perf/roofline.py:313-319](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/perf/roofline.py#L313-L319)：IPC 总能从结尾那行抠到；但 `total_bytes` 依赖 memory 行里有 `read_bytes=`/`write_bytes=` 字样。注意：当前 `perf.cpp` 的 MEM 类 memory 行打印的是 `reqs/r/w/lat`（请求计数），并不含 `read_bytes=`/`write_bytes=`（见 [perf.cpp:596-597](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/sw/runtime/common/perf.cpp#L596-L597)）。因此默认情况下 `total_bytes=None`，算术强度的运行点小红点不会被画出——屋顶曲线和脊点仍正常画出，但定位「运行点」需要你自行估算字节数或传 `--bw`。

绘图函数把配置参数变成屋顶：

```python
peak_flops_per_cycle = 2.0 * cores * threads * fpu_blocks
peak_bw_GBs = args.bw if args.bw is not None else mem_bytes * mem_banks * freq_hz / 1e9
peak_bw_per_cycle = peak_bw_GBs * 1e9 / freq_hz if args.freq > 0 else mem_bytes * mem_banks

flops_per_cycle = flops_total / cycles
ai = (flops_total / total_bytes) if total_bytes else None
...
roof = np.minimum(peak_bw * ai_range, np.full_like(ai_range, peak_perf))
```

[perf/roofline.py:583-608](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/perf/roofline.py#L583-L608)：`peak_flops_per_cycle` 假设每线程每拍 2 个 FLOP（乘加）、乘以 cores×threads×fpu_blocks；`roof` 是斜线 `bw·AI` 与水平线 `peak_perf` 取最小值；`ai` 仅当有字节数时存在，决定是否画运行点。

`fast` 策略是搜索器的精髓——坐标下降：

```python
for name, _, _, _, _ in KNOBS:
    ...
    candidates = [...每个候选值...]
    results = runner.run_batch([t for _, t in candidates])
    for (v, _), (cfg_r, ipc_r, rest_r) in zip(candidates, results):
        if ipc_r > best_ipc:
            best_ipc = ipc_r; cur = cfg_r; improved = True
if not improved:
    break   # 一整轮无改进 → 收敛
```

[perf/roofline.py:535-560](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/perf/roofline.py#L535-L560)：按 KNOBS 顺序逐个旋钮扫、固定其它、取更优者，一轮全扫无改进即停。`run_batch` 在 `--jobs>1` 时并行跑（用 `--nohup` 隔离 build 目录），并把结果写进持久缓存（`--cache`，JSONL 追加，删文件即重来）。

#### 4.3.4 代码实践

1. **目标**：为 sgemm 画一张 roofline 图并定位瓶颈。
2. **步骤**（从 `build/` 目录）：
   - sgemm N=128 的 FLOPs = \( 2N^3 = 2\times128^3 = 4{,}194{,}304 \)（与 [perf/roofline.py:19](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/perf/roofline.py#L19) 注释一致）。
   - 跑单次并画图：
     ```bash
     /usr/bin/python3 ../perf/roofline.py --driver=simx --app=sgemm \
       --args="-n128" --flops=4194304 --plot --output=sgemm128.png
     ```
   - （可选）小范围搜参：
     ```bash
     /usr/bin/python3 ../perf/roofline.py --driver=simx --app=sgemm \
       --args="-n128" --flops=4194304 --config=fast --plot --output=sgemm128_best.png \
       --threads=4,8 --warps=4,8 --fpu-blocks=1,2
     ```
3. **需要观察的现象**：PNG 里斜线与水平线的交点（ridge）；标题里印的 IPC 与配置；若运行点（红点）出现，看它在 ridge 左侧（带宽受限）还是右侧（算力受限）。
4. **预期结果**：sgemm 算术强度较高，运行点一般落在 ridge 右侧（算力受限），IPC 受 fpu_blocks / issue_width 限制。若没出现红点，说明 memory 行未提供字节数（见 4.3.3 的注意点），可手动用 `--bw` 给定带宽后仍能看到屋顶。
5. 运行结果：待本地验证。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `validate()` 要求 `issue_width ≤ warps` 且 `fpu_blocks ≤ issue_width`？
  - **答**：一拍最多从 `issue_width` 个发射槽发出指令，每个槽要绑定一个 warp，故 `issue_width` 不能超过 warp 总数；而每类功能单元（fpu_blocks 等）一拍最多贡献 `issue_width` 条指令，块数再多也用不满，所以约束 `*_blocks ≤ issue_width`。
- **练习 2**：在 `--config=fast` 下，KNOBS 表里 `threads` 排在 `fpu_blocks` 之前意味着什么？
  - **答**：坐标下降按表顺序扫描，`threads` 先被锁定到局部最优，再扫 `fpu_blocks`。表的顺序就是「优先级假设」——作者认为线程数/warp 数/issue 宽度对 IPC 的影响通常大于 cache 微参。
- **练习 3**：不传 `--plot`，roofline.py 还会跑仿真吗？它还输出什么？
  - **答**：会。`--plot` 只控制最后是否画图；脚本仍会按策略跑 blackbox、解析 IPC、打印 `WINNING CONFIGURATION` 块（IPC/instrs/cycles/配置/复现命令）。`--flops` 仅在 `--plot` 时必需（见 [roofline.py:644-645](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/perf/roofline.py#L644-L645)）。

---

### 4.4 perf_gate 性能回归基线纪律

#### 4.4.1 概念说明

`perf_gate` 是和 `model_parity`（[u7-l4](u7-l4-model-parity.md)）并列的一道 CI 门控，但它管的是**性能不回归**而非功能一致。做法很简单：在 rtlsim 上跑一组固定 benchmark，把得到的 `cycles` 和一份「黄金基线」比较，超出容差就红灯。

核心纪律（[AGENTS.md §4](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/AGENTS.md#L88)）：**基线是黄金数据，绝不手改，也绝不靠「把数字调大」来让红灯变绿**。基线只能由人手工跑 `pytest --update-baselines` 重新生成（且 CI 永远不带这个 flag）。

#### 4.4.2 核心流程

```text
perf_gate case (check: perf_gate, 32-bit only)
   │
   ├─ rtlsim 跑 benchmark → (instrs, cycles)
   ├─ 若 --update-baselines：写入 ci/perf/baselines/<cat>.json 并返回
   └─ 否则比较：
         assert config_hash 一致        # 否则「基线过期」
         assert instrs == baseline      # 否则「workload 变了」
         ratio = cycles / baseline.cycles
         assert ratio ≤ 1 + 2%         # 上界：回归门
         assert ratio ≥ 1 - 2%         # 下界：ratchet 棘轮
```

容差是 **±2%**（[ci/perf_baseline.py:23](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/perf_baseline.py#L23) `TOLERANCE = 0.02`）。上界是大家熟悉的「不许变慢」；下界 `ratchet`（棘轮）很关键：**程序显著变快了也红灯**——你必须更新基线把这次提速「锁住」，否则以后偷偷退化回老数字就发现不了。

#### 4.4.3 源码精读

`_perf_gate` 函数把整条纪律编码成断言：

```python
cat = pb.load(case.category)
base = cat.get(case.id)
assert base, "...: no perf baseline — run pytest --update-baselines"
assert base.get("config_hash") == pb.config_hash(case), \
    "...: baseline stale (run config changed) — regenerate with --update-baselines"
ref = base.get(str(xlen))
assert ref, "...: no perf baseline at xlen {} — regenerate"
assert instrs == ref["instrs"], \
    "...: workload changed (instrs ... != baseline ...) — regenerate baseline"
ratio = cycles / float(ref["cycles"])
assert ratio <= 1 + pb.TOLERANCE, "...: perf REGRESSION — cycles ... exceed baseline ..."
assert ratio >= 1 - pb.TOLERANCE, \
    "...: perf IMPROVEMENT of ... beyond ratchet — rerun --update-baselines to lock the gain in ..."
```

[ci/test_runner.py:71-91](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L71-L91)：四道断言依次是「基线存在」「配置指纹一致」「workload（instrs）没变」「cycles 在 ±2% 内」。任何一道失败都会给出明确的修复指引。

基线文件本身就是黄金数据：

```json
"perf_gate:sgemm:rtlsim": {
  "32": { "cycles": 5957013, "instrs": 2494480 },
  "app": "sgemm",
  "args": "-n128",
  "config_hash": "e11d7e47eabd5963",
  "configs": ""
}
```

[ci/perf/baselines/perf_gate.json:22-31](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/perf/baselines/perf_gate.json#L22-L31)：每个 case 存 cycles/instrs、app、args、`config_hash`（配置指纹，变了说明基线对不上当前配置）和 `configs`。`perf_baseline.py` 的文档串说得很直白：

```python
# Perf-regression tolerance: cycles must stay within +/-2% of baseline. The
# upper bound is the regression gate; the lower bound is the ratchet — an
# improvement beyond it must update the baseline so the gain is locked in and a
# later silent regression back toward the old number is still caught.
TOLERANCE = 0.02
```

[ci/perf_baseline.py:19-23](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/perf_baseline.py#L19-L23)：注释明确解释了为什么下界也是门——锁住改进，防退化。

注意 perf_gate 是 32 位专属门控：

```python
# model_parity / perf_gate are 32-bit-only gates: the SimX timing model ...
if self.check in ("model_parity", "perf_gate"):
```

[ci/testcase.py:80-85](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/testcase.py#L80-L85)：这两道门只在 xlen=32 跑，因为 SimX 时序模型以 32 位为基准对齐。

#### 4.4.4 代码实践

1. **目标**：理解基线如何被生产与消费，以及为什么不能手改。
2. **步骤**（源码阅读型，不改代码）：
   - 打开 [ci/perf/baselines/perf_gate.json](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/perf/baselines/perf_gate.json)，找到 `sgemm`（`-n128`，默认配置）的基线 `cycles=5957013`。
   - 读 [ci/test_runner.py:63-91](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/ci/test_runner.py#L63-L91)，模拟「假设你优化了 dcache 让 cycles 降到 5,700,000（约 -4.3%）」——判断它会触发哪条断言。
3. **需要观察的现象**：-4.3% 超过 -2% 下界，触发 `perf IMPROVEMENT ... beyond ratchet`。
4. **预期结果**：红灯，提示你要 `--update-baselines` 把这次提速锁进基线。
5. 运行结果：无需运行，纯断言推理。

#### 4.4.5 小练习与答案

- **练习 1**：你改了一个仲裁器让某 benchmark 快了 5%。CI 的 perf_gate 红了，你该怎么正确处理？能不能直接把 JSON 里的 cycles 改小？
  - **答**：不能手改 JSON。正确做法是确认提速是预期的、且（如 AGENTS.md 要求）已同步更新 SimX 时序模型以维持 model_parity，然后人工跑 `pytest ci -m perf_gate --update-baselines` 重新生成基线，把 diff 放进 PR 供评审。CI 永远不跑 `--update-baselines`。
- **练习 2**：`config_hash` 校验失败说明什么？为什么单独查 `instrs == baseline`？
  - **答**：`config_hash` 由当前配置算出，不一致说明「跑基线时的微架构配置和现在不一样」（例如 `VX_config.toml` 改了 `NUM_THREADS`），基线已不可比，必须重新生成。单独查 `instrs` 是因为 workload 变了（kernel 改了、`-n` 变了）会让 cycles 变化与性能无关，必须先排除掉，剩下的 cycles 差异才有意义。

---

### 4.5 Perfetto：计数器视角的时序维度补充

#### 4.5.1 概念说明

`perf.cpp` 给的是「整个程序跑完后的聚合百分比」——它告诉你调度器整体空了 30%，但不知道**哪一段时间**在空。`docs/perfetto_analysis.md` 描述的工具链（`vortex_perfetto.py` + Perfetto UI）补上了**时间轴维度**：把 `--debug` 生成的逐周期 trace 转成 Chrome Trace JSON，在 Perfetto 里按 warp/核/缓存级查看每个周期的状态。

本讲关注它与 perf 计数器最相关的部分——**warp 状态计数器**：`active` / `stalled` / `active_threads`。它们正是 4.2 节 `active_warps / stalled_warps / issued_threads` 的逐周期时间序列版本。

#### 4.5.2 核心流程

```text
程序带 --debug=N 跑 → run_simx.log / run_rtlsim.log（逐周期状态行）
   │
   ├─ vortex_perfetto.py run.log -t simx -c -o vortex.perfetto.json.gz
   │     ├─ 解析每条指令的 UUID → 在 warp track 上画一条 async slice（首观测阶段 → commit）
   │     ├─ 调度器 warp 状态更新 → per-warp "state" track 的 counter：active/stalled/active_threads
   │     └─ cache/memory 行 → icache/dcache/l2/l3/mem track 的 instant 标记
   │
   └─ Perfetto UI 打开 → 放大低吞吐区间 → 找最长指令 slice / 高 stalled 区间
```

关键术语：**UUID** 是调度器在 schedule 阶段给每条指令分配的全局唯一编号（[u13-l2](u13-l2-debugging.md) 已详述），它是连接指令、cache 事件、CSV 行的「主键」。

#### 4.5.3 源码精读

Perfetto 文档列出的 warp 状态计数器与 perf.cpp 的对应关系：

```markdown
Scheduler warp-state updates are emitted as counters on a per-warp "state" track:
- `active`
- `stalled`
- `active_threads` (derived from `tmask`)

These are useful for quickly spotting underutilization vs widespread stalling.
```

[docs/perfetto_analysis.md:118-124](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/perfetto_analysis.md#L118-L124)：`active`/`stalled` 对应 perf.cpp 的 `active_warps`/`stalled_warps`，`active_threads` 由 `tmask` popcount 得到、对应 `issued_threads`。区别是：perf.cpp 是「求和后除以 cycles 得百分比」，Perfetto 是「逐周期画在时间轴上」。

文档给出的判定经验直接映射到 4.2 的指标：

```markdown
- **active low** across warps: not enough runnable work / warps disabled / short kernel.
- **stalled high** across warps: waiting on dependencies or long-latency events.
- **active_threads low**: divergence or masking is limiting effective throughput.
```

[docs/perfetto_analysis.md:166-170](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/perfetto_analysis.md#L166-L170)：`active low` = occupancy 低、`stalled high` = 某级停顿、`active_threads low` = simt_util 低（分支发散）。和 4.2 的 `occupancy%`、`stalls`、`simt_util%` 是同一组概念的两副面孔。

文档还给了关联长延迟指令与 cache 活动的工作流：

```markdown
1. From the long instruction, copy its `uuid`.
2. Use Perfetto search to find events containing that `uuid`.
3. Inspect cache/memory instants across levels (`dcache`, `l2`, `l3`, `mem`) ...
```

[docs/perfetto_analysis.md:157-163](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/perfetto_analysis.md#L157-L163)：用 UUID 把一条慢指令和它触发的各级 cache miss 串起来——这正是把 4.2 的「`load_lat` 大」定位到具体 PC / 具体地址的方法。

#### 4.5.4 代码实践

1. **目标**：用 Perfetto 时间轴验证 4.2 里看到的某个聚合停顿。
2. **步骤**（从 `build/` 目录）：
   - 带 debug 跑 sgemm：`./ci/blackbox.sh --driver=simx --app=sgemm --args="-n128" --debug=3`。
   - 转换：`/usr/bin/python3 ../ci/vortex_perfetto.py run_simx.log -t simx -c -o sgemm.perfetto.json.gz`（脚本与用法见 [docs/perfetto_analysis.md:35-37](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/perfetto_analysis.md#L35-L37)；大日志用 `--cycle-min/--cycle-max` 截窗）。
   - 在 Perfetto UI 里展开 `Vortex GPU 1`，搜 `warp0`，看它的 `state` track。
3. **需要观察的现象**：`stalled` 计数器高的区段是否对应 4.2 报告里占比最高的那个停顿源；`active_threads` 低的区段是否有分支发散。
4. **预期结果**：聚合百分比（4.2）与时间轴形态（4.5）互相印证——例如 4.2 显示 `lsu` 停顿高，Perfetto 里应能看到成片的 `stalled` 区段且伴随 `dcache:miss` 标记。
5. 运行结果：待本地验证（trace 转换细节见 [u13-l2](u13-l2-debugging.md)）。

#### 4.5.5 小练习与答案

- **练习 1**：perf.cpp 的 `simt_util%` 偏低，在 Perfetto 里你应该看哪条计数器曲线？
  - **答**：per-warp `state` track 上的 `active_threads`。它逐周期反映有效线程数；持续低位说明 `tmask` 经常只有少数位为 1，即分支发散或线程被禁用。
- **练习 2**：为什么 Perfetto 关联 cache 事件是「best effort」？
  - **答**：见 [docs/perfetto_analysis.md:162-163](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/perfetto_analysis.md#L162-L163)——部分 cache/memory 行（背景 fill/writeback/evict）不携带 UUID，无法关联回具体指令；只有带 UUID 的请求类事件才能精确定位。

---

## 5. 综合实践

把四组指标串起来，给 sgemm 做一次完整的性能画像。

1. **采集聚合指标**（从 `build/`）：
   ```bash
   ./ci/blackbox.sh --driver=simx --app=sgemm --args="-n128" --perf=1 | tee sgemm_core.log
   ./ci/blackbox.sh --driver=simx --app=sgemm --args="-n128" --perf=7 | tee sgemm_mem.log
   ```
   - 从 `sgemm_core.log` 读 `scheduler:`（idle/occupancy/simt_util）、`stalls:`、`inst_mix:`、结尾 `IPC`。
   - 从 `sgemm_mem.log` 读 `memory:`（reqs/lat）、`dcache:`（命中率，若开 dcache）、`coalescer:`（合并效率）。
2. **画 roofline**：
   ```bash
   /usr/bin/python3 ../perf/roofline.py --driver=simx --app=sgemm \
     --args="-n128" --flops=4194304 --plot --output=sgemm128.png
   ```
3. **归因**：用 4.2 的口径写一句话结论。例如——
   - 若 `IPC` 低 + `stalls: lsu` 高 + `memory: load_lat` 大 + `dcache` 命中率低 → 带宽/命中率受限（roofline 运行点在 ridge 左侧）。
   - 若 `IPC` 低 + `stalls: alu/fpu` 高 + `inst_mix: fpu` 高 + `load_lat` 小 → 算力受限（运行点贴近水平屋顶，应增大 `fpu_blocks`/`issue_width`）。
4. **验证一处细节**：用 4.5 的 Perfetto 流程，放大聚合报告里最严重的那段停顿，确认时间轴形态与百分比一致。
5. **记录**：把结论、IPC、关键停顿源、roofline 图存档，作为后续微架构改动的对照基线（但不要混淆本讲的「自测基线」与 4.4 的 CI 黄金基线——后者不可手改）。

预期：你能用「聚合百分比 + roofline 定性 + Perfetto 定位」三层信息，回答「这个 kernel 慢在哪、为什么、该调哪个旋钮」。运行结果：待本地验证。

---

## 6. 本讲小结

- Vortex 性能计数器是**两段式使能**：编译期 `-DPERF_ENABLE`（由 `blackbox.sh --perf` 注入）控制硬件插桩，运行期 `VORTEX_PROFILING` 选择打印哪一类（`VX_DCR_MPM_CLASS_*`，1=core…16=dxa）。
- `perf.cpp` 的 `vx_device_dump_perf` 把裸 64 位计数器算成四组指标：调度器利用率（idle/occupancy/simt_util）、流水线停顿（fetch/ibuf 用 cycles、后端用 cycles×issue_width）、指令混合、内存延迟（累计延迟÷次数）。
- 全局 IPC 用「`instrs` 求和、`cycles` 取最大」口径，与 u7-l4 的 model_parity 一致——结尾那行 `PERF: instrs=..., IPC=...` 正是 roofline.py 解析的目标。
- `perf/roofline.py` 是旋钮搜索器 + 绘图器：把 31 个 `VX_CFG_*` 旋钮当搜索空间（single/random/fast/best 四策略），按 IPC 选优，并画 roofline（PeakCompute=2·cores·threads·fpu_blocks，Ridge=PeakCompute/PeakBW）。注意其 bytes 正则与当前 memory 行不直接匹配，运行点小红点默认可能不画出。
- `perf_gate` 是 ±2% 容差的双向门：上界防回归、下界（ratchet）锁改进；基线是黄金数据，只能由人 `pytest --update-baselines` 重生成，绝不手改。
- `perf.cpp`（聚合百分比）与 Perfetto（逐周期 warp 状态 active/stalled/active_threads）是同一组性能现象的「汇总视图」与「时间轴视图」，二者配合才能既知「慢多少」又知「慢在哪一段」。

## 7. 下一步学习建议

- 想深入「停顿从哪来」：回到 [u6-l1](u6-l1-simx-scheduler.md)（调度器/屏障）与 [u6-l3](u6-l3-simx-issue.md)（记分板/操作数收集），对照本讲的 `stalls:` 各项看每级流水线的实现。
- 想深入内存延迟与命中率：读 [u8-l2](u8-l2-cache-internals.md)（cache tags/MSHR）与 [u8-l3](u8-l3-mem-fabric.md)（访存合并/DRAM 模型），把 `load_lat`/`dcache hit%`/`coalescer misses` 对应到硬件结构。
- 想理解 CI 如何把 perf_gate 与 model_parity 一起调度：进入 [u13-l4](u13-l4-ci-parity.md)（CI 与 model_parity 门控），以及 [docs/designs/continuous_integration.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/designs/continuous_integration.md)。
- 想用 trace diff 排查性能异常：复习 [u13-l2](u13-l2-debugging.md)（调试追踪与 SimX-as-oracle），把本讲的 Perfetto 时间轴与 trace_csv 的 UUID 主键串起来。

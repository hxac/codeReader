# GPU Kernel 调试与 PTX/SASS

## 1. 本讲目标

本讲是 FA4（`flash_attn/cute/`）专家层的最后一篇，回答一个工程问题：**当 kernel 跑错、跑挂（hang）或被 racecheck 报错时，该怎么查？**

FA4 的 kernel 是 Python+CuTeDSL 在运行时 JIT 编译出来的（见 [u11-l1](u11-l1-jit-and-cache.md)），你既看不到传统 C++/CUDA 那样的源码，也不能像调试 CPU 程序那样随便打断点。但仓库提供了一套完整的调试工具链：

- 设备端 `cute.printf` + `FA_LOG_LEVEL`：在 kernel 里定点打印，定位「卡在哪一步」；
- `compute-sanitizer --tool=racecheck`：检查共享内存竞争（但要警惕假阳性）；
- `CUTE_DSL_KEEP_PTX` / `CUTE_DSL_LINEINFO` / `cute_dsl_ptxas.py`：把编译产物 PTX/SASS 落盘，肉眼核对指令；
- `AI/` 目录下的排查文档：把前人踩过的坑（2CTA 死锁、TMA 假阳性、CLC 调度异常）整理成方法论。

学完本讲你应该能够：

1. 用带线程守卫的 `cute.printf` 做二分定位，找出 kernel 卡死或算错的位置；
2. 理解 `compute-sanitizer racecheck` 的输出，并识别 `cp.async.bulk` 带来的假阳性；
3. 用环境变量导出 PTX/SASS，并在其中找到关键指令（如 online softmax 的 `ex2`、`rescale`）；
4. 把 `AI/` 文档当作「现场手册」，按图索骥排查 2CTA 死锁与 CLC 调度异常。

## 2. 前置知识

本讲默认你已经读过以下讲义（术语不再重复解释）：

- [u5-l3 命名屏障与 warp 同步](u5-l3-named-barriers.md)：命名屏障、mbarrier、旗标三类同步原语；
- [u8-l1 Blackwell 前向 Kernel 全景](u8-l1-blackwell-forward.md)：UMMA、tmem、persistent kernel；
- [u8-l4 hd256 2CTA 专用 Kernel](u8-l4-hd256-2cta-kernel.md)：2CTA cluster 协作、`tx_count` 翻倍、死锁陷阱；
- [u11-l1 JIT 编译与缓存机制](u11-l1-jit-and-cache.md)：`cute.compile` 把 Python 编译为 PTX→CUBIN 的链路、`compile_key`、`is_fake_mode()`。

补两个本讲要用到的背景概念：

- **PTX**：NVIDIA 的并行线程执行中间指令集（ISA）。它是「机器无关」的，把不同代 GPU 的指令抽象成统一文本。FA4 的 CuTeDSL 编译产物就是 PTX 文本。
- **CUBIN / SASS**：`ptxas` 把 PTX 进一步编译成 CUBIN（GPU 二进制），CUBIN 里实际执行的机器码叫 SASS（Streaming Assembler）。`cuobjdump -sass` 或 Triton 的反汇编工具能把 CUBIN 还原成可读的 SASS 文本。FA4 调试时通常停在 PTX 这一层（人能读懂、又能反映编译器决策）；需要追到寄存器分配、指令调度时才往下看 SASS。

一句话层级关系：

```
Python (CuTeDSL)  ──cute.compile──►  PTX (文本)  ──ptxas──►  CUBIN (二进制) ──反汇编──► SASS
```

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`flash_attn/cute/fa_logging.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/fa_logging.py) | 统一日志门面：`FA_LOG_LEVEL` 控制宿主/设备日志，`fa_log` 走 Python logging，`fa_printf` 走设备端 `cute.printf` 并被 `const_expr` 编译期裁剪。 |
| [`flash_attn/cute/cute_dsl_ptxas.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_ptxas.py) | 用系统 `ptxas` 替换内嵌 `ptxas` 的补丁，顺带把 PTX/CUBIN 落盘。由 `CUTE_DSL_PTXAS_PATH` 触发。 |
| [`flash_attn/cute/cute_dsl_utils.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_utils.py) | `dump_kernel_attributes()`：从 CUBIN 反查寄存器数、本地内存大小；尝试导入 Triton 反汇编器做 SASS。 |
| [`flash_attn/cute/interface.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py) | 在导入时自动应用 `cute_dsl_ptxas.patch()`；把 `get_fa_log_level()` 纳入 `compile_key`。 |
| [`flash_attn/cute/flash_fwd_sm100.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py) | Blackwell 前向 kernel；含 CLC 调度 trace 的设备端 emit 点（带线程守卫的 `fa_printf`）。 |
| [`flash_attn/cute/softmax.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py) | online softmax，本讲综合实践的「靶子」——定位其中 `exp2`/`rescale_O` 对应的 PTX 指令。 |
| [`AI/DEBUG_2CTA.md`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/DEBUG_2CTA.md) | 2CTA/pipeline kernel 卡死的七步排查法与典型陷阱。 |
| [`AI/RACECHECK_TMA_HAZARD.md`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/RACECHECK_TMA_HAZARD.md) | `cp.async.bulk`（裸地址 TMA）触发 racecheck 假阳性的根因与证明。 |
| [`AI/CLC_TRACE_DEBUG.md`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/CLC_TRACE_DEBUG.md) | 如何用 `FA_LOG_LEVEL=3 FA_CLC=1` 抓 CLC 调度 trace 并用 `parse_clc_log.py` 解析。 |

## 4. 核心概念与源码讲解

### 4.1 cute.printf 定点调试

#### 4.1.1 概念说明

GPU 上没有 `print` 调试器那种「单步执行」，最常见的设备端调试手段就是 **`cute.printf`**：在 kernel 里插一条打印语句，运行时把信息从 GPU 回传到宿主 stdout。它廉价、不需要符号调试器，是定位「kernel 卡在哪、变量值对不对」的首要工具。

但直接在 kernel 里全线程 `printf` 会产生**打印风暴**——一个 launch 可能上百万个线程，每个都打印一行，既刷屏又拖慢运行。所以 FA4 给 `cute.printf` 配了两道「闸门」：

1. **线程守卫（thread guard）**：只让少量代表性线程打印（每 warp 一个、每 CTA 一个、或指定线程）；
2. **日志等级（`FA_LOG_LEVEL`）**：在编译期就把「关掉的」printf 整条删掉，零运行开销。

第二点是 FA4 区别于普通 CUDA 代码的关键：因为 CuTeDSL 是 JIT 编译的，`fa_printf` 用 `const_expr` 把等级判断烘焙成编译期常量，等级不够时这条 printf 在生成的 PTX 里**根本不存在**，不会像 C 宏那样留下一个空调用。

#### 4.1.2 核心流程

`cute.printf` 排查卡死（hang）的标准二分流程（来自 [`AI/DEBUG_2CTA.md`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/DEBUG_2CTA.md)）：

1. **造最小复现**：batch=1、nheads=1、能挂的最小 seqlen，单 config，配合超时/compute-sanitizer 区分「真挂」和「慢」。
2. **由粗到细插 printf**：
   - 第一轮：在每个 warp 主函数（load/mma/softmax/correction/epilogue）的入口/出口打印 → 定位**哪个 warp 卡了**；
   - 第二轮：在每个流水握手点（`consumer_wait`/`producer_acquire`）前后打印 → 定位**卡在哪把屏障**；
   - 第三轮：打印 barrier index、phase、stage → 理解**流水状态**。
3. **识别死锁环**：挂起一定是个循环依赖，典型链是「MMA 等 K → load 已完成但卡在 producer_tail（等 MMA 释放 empty）→ MMA 没法释放因为还在等 K」。看到哪把屏障卡住，就反查「谁该给它发信号、为什么没发」。
4. **系统化变规模**：用不同 seqlen/n_blocks 找规律（如「n_blocks ≤ kv_stages 就好，绕回来就挂」→ 多半是 `tx_count` 或 phase 跟踪问题）。

线程守卫的写法（避免风暴）：

```python
# 每个 warp 只让 1 个线程打印
if cute.arch.thread_idx()[0] % 32 == 0:
    cute.printf("...")

# 每个 CTA 只让 1 个线程打印（elect_one 是上下文管理器，不是 bool）
with cute.arch.elect_one():
    cute.printf("...")

# 指定线程
if tidx == 0:
    cute.printf("...")
```

#### 4.1.3 源码精读

**日志门面 `fa_logging.py`**。FA4 把所有日志收敛到一个模块，用单一环境变量 `FA_LOG_LEVEL` 控制。等级映射在文件头部的文档串和常量里写得很清楚——0 关、1 只宿主、2 宿主+精炼设备 trace、3 全量设备 trace（吵且有性能损耗）：

[`flash_attn/cute/fa_logging.py:9-26`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/fa_logging.py#L9-L26) 说明「设备端 printf 在等级不足时被 `const_expr` 编译期消除，关闭时零开销，改等级需重编译」。

等级解析与全局状态在导入时就固化（这也是为什么改等级要趁早，见 [u2-l2](u2-l2-arch-dispatch-and-config.md) 关于 `lru_cache` 的教训）：

[`flash_attn/cute/fa_logging.py:35-48`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/fa_logging.py#L35-L48) —— `_LOG_LEVEL_NAMES` 给出 `{"off":0,"host":1,"kernel":2,"max":3}`，`_fa_log_level` 在模块加载时由环境变量读入。

宿主侧 `fa_log` 只是普通 Python logging 过滤：

[`flash_attn/cute/fa_logging.py:90-92`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/fa_logging.py#L90-L92) —— `if _fa_log_level >= level: _logger.info(msg)`，纯运行期判断，不进 kernel。

设备侧 `fa_printf` 才是关键，它把等级判断包进 `const_expr`：

[`flash_attn/cute/fa_logging.py:95-97`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/fa_logging.py#L95-L97) —— `if const_expr(_fa_log_level >= level): cute.printf(fmt, *args)`。`const_expr` 在编译期求值，条件为假时整条 `cute.printf` 连同它的参数运算都被删除，生成的 PTX 里不留痕迹。

> 这条 `const_expr` 还有第二个工程后果：因为 `_fa_log_level` 的值会影响**生成的代码**，它必须进 `compile_key`，否则换等级会复用旧产物。FA4 确实这么做了——见下方「等级入键」。

**等级参与 compile_key**。`interface.py` 在组装前向的编译键时，最后一项就是当前日志等级：

[`flash_attn/cute/interface.py:760-767`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L760-L767) —— compile_key 元组末尾是 `fa_logging.get_fa_log_level()`。所以「开 debug 跑一次」和「关 debug 跑一次」会编译出**两份不同的 kernel**，这与 [u11-l2](u11-l2-constexpr-specialization.md) 讲的「凡改生成代码的参数都进键」完全一致。

**真实 emit 点：CLC trace**。Blackwell 前向 kernel 里，CLC 调度器每次领活时由「调度 warp」打印一行 trace，就是一个教科书级的「带线程守卫 + 带等级 + 带坐标」的 `fa_printf` 用法：

[`flash_attn/cute/flash_fwd_sm100.py:2946-2953`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L2946-L2953) —— 先用 `cute.arch.thread_idx()[0] == self.clc_scheduler_warp_id * cute.arch.WARP_SIZE` 只让**调度 warp 的 lane 0** 打印，再以等级 3 调 `fa_printf`，输出 `smid`、`block_idx`、`work_tile` 的 `(m_blk,h,b,s)` 与 `valid`。这正是 [`AI/CLC_TRACE_DEBUG.md`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/CLC_TRACE_DEBUG.md) 里那行 `[CLC] query ...` 的来源。

对应的宿主侧摘要（确认 CLC 到底有没有被选中）：

[`flash_attn/cute/flash_fwd_sm100.py:252`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L252) —— `fa_log(1, f"TileScheduler=..., scheduling_mode={...}, USE_2CTA={...}")`，等级 1 即宿主侧输出，不需要 kernel printf。

#### 4.1.4 代码实践

> 这是一个**源码阅读型 + 待本地验证**的实践（无 GPU 也能做阅读部分）。

1. **实践目标**：理解「等级不够时 printf 被编译期删除」这件事在源码层面是如何发生的。
2. **操作步骤**：
   - 打开 [`fa_logging.py:95-97`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/fa_logging.py#L95-L97)，确认 `fa_printf` 用 `const_expr` 守卫。
   - 打开 [`interface.py:766`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L766)，确认日志等级在 compile_key 末尾。
   - 打开 [`flash_fwd_sm100.py:2946-2953`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L2946-L2953)，看 CLC trace 的线程守卫怎么写。
   - **（需 GPU，待本地验证）** 设 `FA_LOG_LEVEL=3 FA_CLC=1` 跑一次 Blackwell 前向（参考 [`AI/CLC_TRACE_DEBUG.md:26-37`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/CLC_TRACE_DEBUG.md#L26-L37) 给的 heredoc 脚本），重定向到日志文件，再用 `python AI/parse_clc_log.py <log>` 解析。
3. **需要观察的现象**：stdout 出现形如 `[FA] TileScheduler=..., scheduling_mode=CLC, USE_2CTA=False` 的宿主行，以及大量 `[CLC] query sm=.. cta=.. (m_blk=..,h=..,b=..,s=..) valid=..` 的设备行。
4. **预期结果**：`valid=1` 表示该次查询领到了有效工作块，`valid=0` 表示调度器已耗尽。把 `FA_LOG_LEVEL` 降到 2 或 1 再跑，设备端 CLC 行应当**完全消失**（被编译期删除，而非只是不打印）——这正是 `const_expr` 的效果。**待本地验证**。
5. 如果无法运行，明确写「待本地验证」，但能口头解释「为什么等级变化会触发重编译」。

#### 4.1.5 小练习与答案

**Q1**：为什么 `fa_printf` 用 `const_expr` 而不是普通 `if` 来守卫等级？

**答**：普通 `if` 是运行期分支，即使条件为假，`cute.printf` 及其参数计算仍会被编译进 PTX（只是运行时不执行），既有代码体积开销也拖慢 kernel。`const_expr` 让编译器在编译期就删掉整条分支，关闭时生成的 kernel 里**完全没有**这条 printf，是真正的零开销。

**Q2**：在 Blackwell 前向里，CLC trace 为什么只让「调度 warp 的 lane 0」打印，而不是 `elect_one()`？

**答**：CLC trace 要反映**调度器视角**的领活过程，调度由专门的 scheduler warp 负责（`self.clc_scheduler_warp_id`），必须精确锁定那一个 warp 的 lane 0 才能拿到正确的 `work_tile`。`elect_one()` 只是「CTA 内任选一个线程」，可能选到非调度 warp，打印出的 `work_tile` 没有意义甚至未初始化。

---

### 4.2 compute-sanitizer racecheck 与假阳性

#### 4.2.1 概念说明

`compute-sanitizer` 是 NVIDIA 的 GPU 正确性检查工具套件，其中 `--tool=racecheck` 专门检测**共享内存（shared memory）数据竞争**：它给每次 smem 读写插桩，检查是否存在「缺乏公认 happens-before 关系的冲突访问」。对一个 producer/consumer 流水 kernel，这是发现「load 还没写完 consumer 就读」之类 bug 的利器。

但 racecheck 不是真理机器——它对**某些异步拷贝指令的建模是不完整的**，会报出**假阳性（false positive）**：代码本身正确，却被报 race。FA4 在 SM100 反向 kernel 上就撞上了这个坑，[`AI/RACECHECK_TMA_HAZARD.md`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/RACECHECK_TMA_HAZARD.md) 把它完整记录了下来。本节的核心教训是：**看到 racecheck 报错先别急着改代码，先判断是不是假阳性。**

#### 4.2.2 核心流程

判断 racecheck 报错真假的决策树（源自该文档）：

```
racecheck 报 shared memory race
        │
        ├─ 写操作是 cp.async.bulk（裸地址 TMA）吗？
        │     ├─ 是  → 大概率假阳性（见下文根因）
        │     │        用「五条证据」自证，再决定改指令还是忽略
        │     └─ 否 → 当成真 bug 排查
        │
        └─ 五条「假阳性」自证证据（文档 §Proof）：
              1. 改用等价指令结果 bit 一致
              2. 单 warp 复现：0 hazard
              3. 完全展开循环：0 hazard
              4. 每轮加 bar.sync：0 hazard
              5. 换成 cp.async.bulk.tensor（描述符 TMA）：0 hazard
```

关键对照（文档里的指令表）：

| 变体 | PTX 指令 | racecheck |
|------|----------|-----------|
| 裸地址（cta 域） | `cp.async.bulk.shared::cta.global.mbarrier::complete_tx::bytes` | **报 hazard** |
| 裸地址（cluster 域） | `cp.async.bulk.shared::cluster.global.mbarrier::complete_tx::bytes` | **报 hazard** |
| 描述符 1D | `cp.async.bulk.tensor.1d.shared::cta.global.tile.mbarrier::complete_tx::bytes` | 干净 |
| 描述符 2D | `cp.async.bulk.tensor.2d.shared::cta.global.tile.mbarrier::complete_tx::bytes` | 干净 |

#### 4.2.3 源码精读

**根因**。racecheck 给每次 smem 访问插桩并找「缺 happens-before 边的冲突对」。问题出在它如何**归因写者**：

[`AI/RACECHECK_TMA_HAZARD.md:24-38`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/RACECHECK_TMA_HAZARD.md#L24-L38) —— 对 `cp.async.bulk`（裸地址），sanitizer 把这次 smem 写**归因到发起线程**（warp0 的 thread0，经 `elect_one`）。当 warp1 对同一地址发 `ld.shared.b32` 时，sanitizer 找它们之间的同步边——唯一的同步是 warp1 上的 `mbarrier.try_wait.parity` 配合硬件的 `complete_tx::bytes` 完成通知，但 sanitizer **不把这条边建模成跨 warp 的 happens-before**（尤其在动态循环里），于是报 race。

而对 `cp.async.bulk.tensor`（描述符 TMA），写操作由独立的 TMA 硬件单元完成，sanitizer **不把写归因到任何线程**——没有写者线程就没有冲突对，自然不报。这是「裸地址 vs 描述符」的差异，与维度无关（1D/2D 描述符都干净）。

**哪类 buffer 受影响**。文档进一步指出，FA4 的 SM100 反向里只有 **LSE 和 dPsum** 两个 buffer 触发假阳性：

[`AI/RACECHECK_TMA_HAZARD.md:16-22`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/RACECHECK_TMA_HAZARD.md#L16-L22) —— 因为只有 LSE/dPsum 是「TMA 加载、又被线程级 `lds` 消费」的 buffer；Q/K/V/dO 由 UMMA 硬件指令消费，不产生线程级 `lds`，所以从不触发 racecheck。这条经验能帮你迅速判断「这个 race 报错该不该信」。

**怎么复现/证伪**。文档给了两个约 75 行、自包含的最小复现 kernel：

[`AI/RACECHECK_TMA_HAZARD.md:69-84`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/RACECHECK_TMA_HAZARD.md#L69-L84) —— `racecheck_repro_1d_bulk.py`（裸地址）报 1 个错，`racecheck_repro_1d_tensor.py`（描述符）报 0 个；两者流水协议完全一致，只差一条拷贝指令。命令行：

```bash
CUTE_DSL_LINEINFO=1 compute-sanitizer --tool=racecheck python AI/racecheck_repro_1d_bulk.py    # 1 error
compute-sanitizer --tool=racecheck python AI/racecheck_repro_1d_tensor.py                        # 0 hazards
```

注意 `CUTE_DSL_LINEINFO=1` 的作用：让 PTX 带上行号映射（`.loc`），这样 racecheck 的报错能指回 CuTeDSL 源码行（如文档里 `[248 hazards]` 指向 `racecheck_repro_1d_bulk.py:55` 的 `dst[...] = s[...]`），否则只有裸地址偏移（`...+0x770`），难以定位。

**修法**。文档给出的是「把裸地址 TMA 换成描述符 TMA」：

[`AI/RACECHECK_TMA_HAZARD.md:130-144`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/RACECHECK_TMA_HAZARD.md#L130-L144) —— 把 `copy_atom_stats = cute.make_copy_atom(cpasync.CopyBulkG2SOp(), Float32)` 换成用 `cpasync.make_tiled_tma_atom` 配 `CopyBulkTensorTileG2SOp`，生成 `cp.async.bulk.tensor.1d`，流水协议（mbarrier init / arrive_expect_tx / try_wait_parity / consumer_release）原封不动。

> 一个容易踩的坑（文档特别强调）：`--racecheck-memcpy-async=no` **没用**——这个 flag 管的是老的 `cp.async`（sm80），不管 `cp.async.bulk`（TMA），加了 hazard 照报。

#### 4.2.4 代码实践

> 源码阅读型 + 待本地验证。

1. **实践目标**：学会用最小复现 + 五条证据，独立判断一个 racecheck 报错是真是假。
2. **操作步骤**：
   - 读 [`AI/RACECHECK_TMA_HAZARD.md:53-66`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/RACECHECK_TMA_HAZARD.md#L53-L66) 的「五条证据」。
   - 读 [`AI/racecheck_repro_1d_bulk.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/racecheck_repro_1d_bulk.py) 与 [`AI/racecheck_repro_1d_tensor.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/racecheck_repro_1d_tensor.py)，找出二者**唯一**的区别（拷贝原子）。
   - **（需 GPU + compute-sanitizer，待本地验证）** 跑上面两条命令，确认一个报 1 error、一个报 0 hazard。
3. **需要观察的现象**：bulk 版报 `Race reported between Write access ...+0x430 ... and Read access ...+0x770`；tensor 版干净。
4. **预期结果**：能用一句话说出根因——「racecheck 把 `cp.async.bulk` 的 smem 写归因到发起线程，而跨 warp 的 mbarrier 完成通知不被建模为 happens-before；描述符 TMA 由独立硬件单元写、无写者线程故不报」。**待本地验证**实际报错行号。

#### 4.2.5 小练习与答案

**Q1**：为什么 Q/K/V/dO 用 UMMA 消费时不触发 racecheck，而 LSE/dPsum 会？

**答**：racecheck 只插桩**线程级共享内存访问**（`lds`/`sts`）。Q/K/V/dO 由 UMMA 硬件指令（`tcgen05.mma`）直接从 smem/tmem 取操作数，不产生线程级 `lds`，所以没有可插桩的访问、不报 race。LSE/dPsum 则是被计算 warp 用线程级 `autovec_copy`（即 `lds`）从 smem 读出来的，存在线程级读，配合裸地址 TMA 的写就构成了「写者线程 + 读者线程」的冲突对。

**Q2**：有人建议加 `--racecheck-memcpy-async=no` 来消掉 FA4 反向的 race 报错，行不行？

**答**：不行。该 flag 针对的是 sm80 的老 `cp.async` 指令族，对 `cp.async.bulk`（TMA）无效，hazard 会照报。真正有效的做法是改用描述符 TMA（`cp.async.bulk.tensor`），或在每轮迭代加一个 racecheck 认得的 `bar.sync`（但这会拖慢流水，仅用于证伪而非生产修复）。

---

### 4.3 PTX/SASS 导出与 AI 排查文档

#### 4.3.1 概念说明

很多时候光看 Python 源码不够——你怀疑编译器把某段循环优化错了，或者想确认「我写的 `exp2` 到底有没有变成 `ex2` 指令」「`const_expr` 删分支删干净没有」。这时需要看**编译产物**：PTX（文本、人可读、反映编译器在 ISA 层的决策）乃至 SASS（机器码、反映寄存器分配与指令调度）。

FA4 提供三个层次的「落盘」开关，全部是环境变量、零代码改动：

| 环境变量 | 作用 | 产出 |
|----------|------|------|
| `CUTE_DSL_KEEP_PTX=1` | 编译后保留 `.ptx` 文本 | PTX 文件（在 dump 目录） |
| `CUTE_DSL_LINEINFO=1` | PTX 带 `.loc` 行号映射 | 报错/反汇编能指回源码行 |
| `CUTE_DSL_PTXAS_PATH=<path>` | 用系统 `ptxas` 替换内嵌 ptxas | 触发 `cute_dsl_ptxas.py` 补丁，可顺带 `CUTE_DSL_KEEP_CUBIN=1` 留 CUBIN |

注意 FA4 默认是用**内嵌的** ptxas（随 cutlass-dsl 打包）把 PTX 编译成 CUBIN 的，你看不到中间产物。`CUTE_DSL_PTXAS_PATH` 这条路把编译「外置」到系统 ptxas，于是能在中间环节插手——这正是 `cute_dsl_ptxas.py` 做的事。

#### 4.3.2 核心流程

PTX/SASS 调试的典型工作流：

```
设 CUTE_DSL_KEEP_PTX=1（+可选 LINEINFO=1）
        │
        ▼
跑一次目标 kernel（首次调用触发 JIT 编译）
        │
        ▼
在 dump 目录找到 *.ptx（文件名含 function_name）
        │
        ├─ 想「核对我的高级抽象变成了什么指令」→ 直接 grep PTX
        │     例：grep "ex2.approx" 找 online softmax 的换底指数
        │
        └─ 想看寄存器/指令调度 → 再用 SASS
              路线 A：CUTE_DSL_PTXAS_PATH=... + CUTE_DSL_KEEP_CUBIN=1
                       → 系统 ptxas 编译，留 .cubin → cuobjdump -sass
              路线 B：dump_kernel_attributes() → 只查寄存器数/本地内存大小
```

`cute_dsl_ptxas.py` 的补丁逻辑（伪代码）：

```
patch()：要求 CUTE_DSL_KEEP_PTX=1，否则报错
    └─ 替换 CudaDialectJitCompiledFunction._load_cuda_library
         _patched_load_cuda_library(self):
             1. _get_ptx(func)：在 dump 目录按 function_name 找 *.ptx，校验含 .entry
             2. _compile_ptx：用系统 ptxas -arch=<从PTX解析> -O3 编出 cubin
                  └─ 若 CUTE_DSL_KEEP_CUBIN=1，把 .cubin 写盘
             3. cudaLibraryLoadData 加载 cubin，注册到所有 device
             4. 若用户没要 PTX，删掉 .ptx（保持环境干净）
             任一步失败 → 回退到内嵌 ptxas
```

#### 4.3.3 源码精读

**补丁入口：interface.py 导入时自动应用**。FA4 不需要你手动调 `patch()`，只要设了 `CUTE_DSL_PTXAS_PATH`，导入 `interface` 时就自动装上：

[`flash_attn/cute/interface.py:22-26`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L22-L26) —— `if os.environ.get("CUTE_DSL_PTXAS_PATH", None) is not None: from ... import cute_dsl_ptxas; cute_dsl_ptxas.patch()`。这意味着这个开关必须在**导入 flash_attn.cute 之前**就设好（再次印证 [u2-l2](u2-l2-arch-dispatch-and-config.md) 的教训：环境变量要趁早）。

**补丁本体：cute_dsl_ptxas.py**。先看它的环境变量声明：

[`flash_attn/cute/cute_dsl_ptxas.py:1-19`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_ptxas.py#L1-L19) —— 声明四个变量：`CUTE_DSL_PTXAS_PATH`（ptxas 路径）、`CUTE_DSL_PTXAS_VERBOSE`（调试补丁本身）、`CUTE_DSL_KEEP_PTX`/`CUTE_DSL_KEEP_CUBIN`（留产物）、`CUTE_DSL_DUMP_DIR`（产物目录，默认当前工作目录）。

找 PTX 文件靠函数名匹配：

[`flash_attn/cute/cute_dsl_ptxas.py:30-42`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_ptxas.py#L30-L42) —— `_get_ptx` 用 `Path(dump_dir).glob(f"*{func_name}*.ptx")`，读出内容并 `rstrip("\x00")`（去掉尾部 null 字节），再校验含 `.entry ` 且以 `}` 结尾才算合法 PTX。

用系统 ptxas 编译：

[`flash_attn/cute/cute_dsl_ptxas.py:45-78`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_ptxas.py#L45-L78) —— `_compile_ptx` 先用正则 `\.target\s+(sm_\d+[a-z]?)` 从 PTX 里抠出目标架构（抠不到就默认 `sm_90a`），再用 `[CUTE_DSL_PTXAS_PATH, f"-arch={arch}", "-O3", "-o", cubin_tmp, ptx_path]` 调系统 ptxas；`CUTE_DSL_KEEP_CUBIN=1` 时把 cubin 落盘。注意 `-O3`——它影响生成的 SASS 质量。

替换加载逻辑：

[`flash_attn/cute/cute_dsl_ptxas.py:81-129`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_ptxas.py#L81-L129) —— `_patched_load_cuda_library` 是替换函数：先 `_get_ptx`，再 `_compile_ptx`，用 `cudaLibraryLoadData` 加载 cubin 并注册到所有 device（`for dev in range(self.num_devices)`）；若用户原本没要 PTX（`_user_wanted_ptx=False`），加载后删掉 `.ptx` 保持干净。任一步失败都 `_log(...)` 后回退到内嵌 ptxas，**绝不硬崩**。

安装补丁与前置断言：

[`flash_attn/cute/cute_dsl_ptxas.py:132-151`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_ptxas.py#L132-L151) —— `patch()` 先 `assert CUTE_DSL_PTXAS_PATH is not None`、校验文件可执行，再 `assert CUTE_DSL_KEEP_PTX=1`（因为整个补丁的前提就是「PTX 已经被 dump 出来」），最后把 `cls._load_cuda_library = _patched_load_cuda_library` 完成猴子补丁。

**寄存器/资源探查：dump_kernel_attributes**。如果你只想知道「这个 kernel 用了多少寄存器、占多大 local memory」（判断是否寄存器溢出导致性能塌方），不必走完整 SASS 流程，`cute_dsl_utils.py` 提供了轻量查询：

[`flash_attn/cute/cute_dsl_utils.py:126-158`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_utils.py#L126-L158) —— `dump_kernel_attributes` 用 `cuLibraryLoadData` 加载 cubin，经 `cuFuncGetAttribute` 查 `CU_FUNC_ATTRIBUTE_LOCAL_SIZE_BYTES` 和 `CU_FUNC_ATTRIBUTE_NUM_REGS` 并打印。它要求 cubin 已落盘（`compiled_kernel.artifacts.CUBIN` 非空，对应编译时带 `--keep-cubin`）。

SASS 反汇编则靠可选依赖：

[`flash_attn/cute/cute_dsl_utils.py:8-11`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_utils.py#L8-L11) —— `try: from triton.tools.disasm import extract except ImportError: extract = None`。FA4 复用 Triton 的 SASS 反汇编器；没装 Triton 就 `extract=None`，SASS 这条路降级。模块顶部还留了 `load_cubin_module_data_og = ...` 和 `cute_compile_og = cute.compile` 两个原始引用，是给同类猴子补丁留的「还原点」。

**综合实践的靶子：online softmax 的 exp2/rescale**。本讲综合实践要在 PTX 里找 online softmax 的指数/重缩放指令，先看它们在源码里长什么样（见 [u4-l1](u4-l1-online-softmax.md)）。换底后的指数运算：

[`flash_attn/cute/softmax.py:168-181`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py#L168-L181) —— `acc_S_row_exp = cute.math.exp2(acc_S_row * scale_log2 - row_max_cur_scaled, fastmath=True)`，且 `row_scale[r] = cute.math.exp2((row_max_prev - row_max_cur) * scale_log2)`。这两个 `exp2` 在 PTX 里对应 `ex2.approx.f32`（`fastmath=True` 时是近似版）。`row_scale` 正是重缩放因子 \( e^{(m_{\text{old}}-m_{\text{new}})\cdot\text{scale\_log2}} \le 1 \)。

重缩放作用到输出累加器：

[`flash_attn/cute/softmax.py:229-240`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py#L229-L240) —— `rescale_O` 把 `acc_O` 每行乘以 `row_scale`：`acc_O_mn[r,None].store(acc_O_mn[r,None].load() * row_scale[r])`。在 PTX 里这段是一串 `mul.f32`（或融合的 `fma`）。

> 小贴士：找指令时别只搜 `exp2`——FA4 用的是 `cute.math.exp2(..., fastmath=True)`，PTX 里多半是 `ex2.approx.ftz.f32`（近似 + flush-to-zero）。搜 `ex2` 更稳。

#### 4.3.4 代码实践（本讲主线实践）

> 这是 spec 指定的主线实践。**需 GPU，待本地验证**，但阅读部分无 GPU 也能做。

1. **实践目标**：导出一次前向的 PTX，在其中定位 online softmax 的 `ex2`/`rescale` 指令，并说明其作用。
2. **操作步骤**：
   - 准备：找一份系统 `ptxas`（如 `/usr/local/cuda/bin/ptxas`），用 `which ptxas` 或 `nvcc --version` 所在目录定位。
   - 设环境变量后跑前向（参考 [u1-l3](u1-l3-install-and-first-run.md) 的最小调用）：
     ```bash
     export CUTE_DSL_KEEP_PTX=1
     export CUTE_DSL_LINEINFO=1
     export CUTE_DSL_PTXAS_PATH=/usr/local/cuda/bin/ptxas   # 可选，启用系统 ptxas 补丁
     export CUTE_DSL_DUMP_DIR=/tmp/fa_ptx                    # 可选，指定产物目录
     python -c "
     import torch
     from flash_attn.cute.interface import flash_attn_func
     q = torch.randn(1, 512, 8, 64, device='cuda', dtype=torch.float16)
     k = torch.randn(1, 512, 8, 64, device='cuda', dtype=torch.float16)
     v = torch.randn(1, 512, 8, 64, device='cuda', dtype=torch.float16)
     out, lse = flash_attn_func(q, k, v, causal=True)
     torch.cuda.synchronize()
     "
     ```
   - 在 dump 目录找最大那个 `.ptx`（文件名含 `flash_attn_fwd` 之类 function name），用编辑器打开。
   - 搜索指令：`grep -n "ex2" <file>.ptx`、`grep -n "mul.f32" <file>.ptx`、`grep -n ".loc" <file>.ptx | grep softmax`。
3. **需要观察的现象**：
   - 能找到形如 `ex2.approx.ftz.f32 %fXX, %fYY;` 的指令（对应 [`softmax.py:169`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py#L169) 的 `exp2`）；
   - 因为开了 `LINEINFO=1`，这些指令上方应有 `.loc <file> <line> <col>` 注释，能把 PTX 行指回 `softmax.py`；
   - 在 `rescale_O` 对应区域能看到一串 `mul.f32`（[`softmax.py:240`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py#L240) 的逐行乘 `row_scale`）。
4. **预期结果**：用一句话说明作用——`ex2.approx` 把换底后的分数 \( \text{score}\cdot\text{scale\_log2} - m \) 转成 \( P=e^{S-m} \)（行最大值归零、防溢出）；紧随其后的 `mul.f32`（rescale）用因子 \( e^{(m_{\text{old}}-m_{\text{new}})\cdot\text{scale\_log2}} \) 修正旧累加器 `acc_O`，使分块累加在数学上等价于一次性 softmax（见 [u4-l1](u4-l1-online-softmax.md)）。**待本地验证**实际指令助记符与 `.loc` 行号。
5. 若无 GPU：完成「源码阅读型」版本——对照 [`softmax.py:168-181`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py#L168-L181) 与 [`softmax.py:229-240`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/softmax.py#L229-L240)，写出「这两个 `exp2` 和这段 `mul` 会分别编译成什么 PTX 指令」的预测，标注待本地验证。

#### 4.3.5 小练习与答案

**Q1**：为什么 `cute_dsl_ptxas.patch()` 要强制要求 `CUTE_DSL_KEEP_PTX=1`？

**答**：整个补丁的工作前提是「PTX 已经被 dump 到磁盘」——`_get_ptx` 靠磁盘上的 `.ptx` 文件读源码、再用系统 ptxas 编译。如果 `CUTE_DSL_KEEP_PTX` 没开，PTX 不会落盘，`_get_ptx` 找不到文件就直接回退到内嵌 ptxas，补丁形同虚设。所以用 `assert` 把这个隐含前提显式化，避免「设了 PTXAS_PATH 却没设 KEEP_PTX、以为在用系统 ptxas 实际在用内嵌」的静默错误。

**Q2**：导出的 PTX 里 `causal=True` 和 `causal=False` 两份有什么可观察的区别？这验证了 [u11-l2](u11-l2-constexpr-specialization.md) 的哪个结论？

**答**：`causal` 是 `Constexpr`，进 compile_key，两份是**不同**的编译产物。可观察的区别通常是：causal 版的主循环里掩码分支被特化（边界块带掩码谓词/R2P 位图写指令，内部块不带），甚至 n_block 的遍历范围不同（因果裁剪）。这正是 [u11-l2] 讲的「凡改变生成代码的参数都触发特化与重编译」——`causal` 改值会编译出两份 PTX，肉眼可见分支差异。

**Q3**：`dump_kernel_attributes()` 报 `num_regs` 很大（比如 >200），意味着什么？该怎么处理？

**答**：寄存器压力大。每个线程占用寄存器越多，一个 SM 能同时驻留的线程/warp 就越少（寄存器文件总量固定），可能拉低占用率（occupancy）、拖慢性能；若超过硬件上限，ptxas 会把溢出部分放到 local memory（`local_size_bytes > 0`），性能塌方更严重。处理思路：减小 tile 尺寸、减少流水级数、或检查是否引入了过宽的寄存器张量（参考 [u11-l4](u11-l4-benchmark-and-config-search.md) 的 `REG_LIMITS` 配置搜索）。

---

## 5. 综合实践

把本讲三件套串起来，模拟一次真实的「kernel 结果偶发错误」排查。

**场景**：你在 Blackwell 上跑 FA4 反向，`compute-sanitizer --tool=racecheck` 报了 LSE/dPsum 的 shared memory race，但 loss 曲线看着正常，你怀疑是假阳性。

**任务**：设计一套排查方案，按以下顺序运用本讲工具，每步给出「会得到什么信息」：

1. **第一步——用 cute.printf 确认正确性**：在反向 kernel 的 dQ 累加点前后插带线程守卫的 `fa_printf`（`FA_LOG_LEVEL=2`），打印一小块的 `dQ` 局部和，与 PyTorch autograd 参考对比。
   - *预期*：若数值一致，说明「race 是假阳性、结果其实正确」的概率大增。参考 [`AI/DEBUG_2CTA.md:31-41`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/DEBUG_2CTA.md#L31-L41) 的「打印什么」清单（CTA index、loop count、try_wait 成败）。
2. **第二步——用 racecheck 复现并判真假**：按 [`AI/RACECHECK_TMA_HAZARD.md:69-84`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/RACECHECK_TMA_HAZARD.md#L69-L84) 跑 `racecheck_repro_1d_bulk.py`（应报错）与 `racecheck_repro_1d_tensor.py`（应干净），确认你遇到的是「裸地址 TMA 假阳性」这一已知类别。
   - *预期*：五条证据（数据一致、单 warp 干净、全展开干净、加 bar.sync 干净、换描述符 TMA 干净）至少命中两三条，即可判定假阳性。
3. **第三步——导出 PTX 佐证**：设 `CUTE_DSL_KEEP_PTX=1 CUTE_DSL_LINEINFO=1`，在反向 kernel 的 PTX 里 `grep "cp.async.bulk"`，确认 LSE/dPsum 的加载用的是裸地址 `cp.async.bulk`（而非 `cp.async.bulk.tensor`）。
   - *预期*：PTX 里能搜到 `cp.async.bulk.shared::cta.global.mbarrier::complete_tx::bytes`，与文档 §PTX-level analysis 的 HAZARD 行一致；`.loc` 指回 `flash_bwd_sm100.py` 的 LSE/dPsum 加载点。
4. **第四步——定夺**：综合前三步，写下结论：是真 bug 还是假阳性？若是假阳性，按文档修法（换描述符 TMA）还是接受现状？给出理由。

**产出**：一份一页排查报告，含每步的命令、观察、结论。**待本地验证**（需 Blackwell GPU + compute-sanitizer）；无 GPU 时，至少把「每一步该跑什么命令、预期看到什么」写成可执行的 runbook。

## 6. 本讲小结

- FA4 把所有日志收敛到 [`fa_logging.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/fa_logging.py) 的单一 `FA_LOG_LEVEL`：`fa_log` 走宿主 Python logging，`fa_printf` 走设备 `cute.printf` 并被 `const_expr` 编译期裁剪——关掉时零开销，但改等级会因进 `compile_key`（[`interface.py:766`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L766)）触发重编译。
- 设备端 `cute.printf` 排查卡死靠「由粗到细二分 + 线程守卫（`thread_idx%32`、`elect_one`、指定线程）」，[`AI/DEBUG_2CTA.md`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/DEBUG_2CTA.md) 给了七步法与 2CTA 专项陷阱（`tx_count` 漏乘 cluster、空 commit group、producer_tail、tile 坐标漏除、softmax 掩码偏移）。
- `compute-sanitizer --tool=racecheck` 会因 `cp.async.bulk`（裸地址 TMA）报**假阳性**：sanitizer 把写归因到发起线程、又不建模跨 warp 的 mbarrier happens-before；换 `cp.async.bulk.tensor`（描述符 TMA）即干净。`--racecheck-memcpy-async=no` 无效。
- PTX/SASS 落盘靠环境变量三件套：`CUTE_DSL_KEEP_PTX=1` 留 PTX、`CUTE_DSL_LINEINFO=1` 带行号映射、`CUTE_DSL_PTXAS_PATH` 触发 [`cute_dsl_ptxas.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_ptxas.py) 的系统 ptxas 补丁（导入时由 [`interface.py:22-26`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L22-L26) 自动安装），配合 `CUTE_DSL_KEEP_CUBIN=1` 可再下探到 CUBIN/SASS。
- 只查寄存器/资源不必走全流程，用 [`cute_dsl_utils.py:126-158`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/cute_dsl_utils.py#L126-L158) 的 `dump_kernel_attributes()` 即可；SASS 反汇编复用 Triton 的 `disasm.extract`（可选依赖）。
- CLC 调度异常用 `FA_LOG_LEVEL=3 FA_CLC=1` 抓 trace，由 [`flash_fwd_sm100.py:2946-2953`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_fwd_sm100.py#L2946-L2953) 的带守卫 `fa_printf` 产出 `[CLC] query ...` 行，再用 [`AI/parse_clc_log.py`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/parse_clc_log.py) 解析——`AI/` 目录就是 FA4 的「现场手册」。

## 7. 下一步学习建议

本讲是 FA4 学习手册的收官篇。到这里你已经走完了从「朴素注意力为什么慢」到「怎么把一个 Blackwell 2CTA kernel 调试到指令级」的完整路径。后续建议：

1. **回头验证**：挑一两篇你跳过实践的讲义（如 [u8-l4](u8-l4-hd256-2cta-kernel.md) 的 2CTA 死锁），用本讲的 `cute.printf` 二分法 + PTX 导出实际跑一遍，把「纸面验证」变成「真机验证」。
2. **读 `AI/` 全集**：本讲只精读了三篇，目录里还有 [`AI/SASS_MMA_ANALYSIS.md`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/SASS_MMA_ANALYSIS.md)、[`AI/SM90_BLOCK_SIZE_TUNING.md`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/SM90_BLOCK_SIZE_TUNING.md)、[`AI/SM90_R2P_MASKING_SASS.md`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/SM90_R2P_MASKING_SASS.md) 等，它们是「SASS 级深度分析」的范文，能训练你读机器码的能力。
3. **二次开发入门**：试着写一个自定义 `score_mod`（见 [u4-l2](u4-l2-score-mod.md)），用本讲的 PTX 导出确认它被内联进 kernel、并观察改闭包值如何触发重编译（承接 [u11-l2](u11-l2-constexpr-specialization.md)）。
4. **向上游反馈**：若你按 [`AI/DEBUG_2CTA.md:81-94`](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/DEBUG_2CTA.md#L81-L94) 判定某个 hang 是「编译器即 bug 源」（printf 修好、fence 不修好），可对比有/无 printf 的两份 PTX 定位被错误重排的指令，向 CUTLASS DSL / MLIR 上游报 issue。

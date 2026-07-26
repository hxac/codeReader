# SM / 共享内存感知调优

## 1. 本讲目标

本讲是「硬件感知调优与扩展」单元的第一篇。前面你已经能读懂一个 TileLang 算子的骨架、存储层级、循环与规约原语，也读过了 engram 门控的前向数学与 benchmark 设施。本讲把这些知识收束到一个新问题：**算子怎么「知道」自己跑在什么硬件上，并据此决定并行度？**

读完本讲，你应当能够：

1. 说清 `tile_kernels/config.py` 提供的三个硬件接口——`get_device_num_sms`、`get_max_smem_per_sm`、`get_num_sms`/`set_num_sms`——各自做什么、为什么前两个带 `lru_cache` 而后两个不带。
2. 跟着 engram 前向 kernel 的占用启发式 `_choose_num_persistent_blocks`，对给定的 `hidden_size` 手算出「持久化块数」，并解释这条公式里每一项的物理含义。
3. 理解 `num_sms` 在 TileKernels 里是**编译期参数**而非运行时读取——因此 `set_num_sms` 会触发重新编译、改变网格形状，只改性能不改正确性。
4. 会用 `generate_num_sms` 在测试里扫描 SM 数，并用 `set_num_sms` 限制可用 SM 跑 benchmark，观察性能随并行度的变化曲线。

## 2. 前置知识

本讲假设你已经掌握以下概念（来自前置讲义），这里只做一句话回顾：

- **SM（Streaming Multiprocessor，流式多处理器）**：GPU 的计算单元，一张卡有若干个（如 H100 有 132 个）。同一个 SM 上可以驻留多个线程块（CTA/block）。「SM 数」基本决定了 GPU 能同时常驻多少个 block，是并行度的硬件上限。参见 u1-l1。
- **共享内存（Shared Memory / SMEM）**：每个 SM 内部一块低延迟片上存储，所有线程块共享。每个 SM 能分给「同时驻留的若干 block」的 SMEM 总量有上限（`shared_memory_per_multiprocessor`）。block 用 SMEM 越多，单个 SM 能同时驻留的 block 数就越少——这就是「占用率（occupancy）」。参见 u2-l2。
- **持久化 kernel（persistent kernel）**：一种调度风格——网格维度绑定**硬件 SM 数**而非数据规模，每个 block 在一个 `Serial` 循环里不重不漏地分片处理全部数据，跨 token 复用常驻数据（如权重）。engram 前向就是这种风格。参见 u6-l1。
- **benchmark 设施**：`benchmark_timer`（CUPTI 计时，返回微秒）+ `count_bytes`（统计读+写字节）算有效带宽 `bandwidth_gbs = num_bytes / t_us / 1e3`。参见 u9-l2。
- **TileLang 的「编译期 vs 运行时」切分**：`@tilelang.jit` 装饰的 kernel 构造函数的参数是**编译期参数**（被烤进编译产物，不同取值各自特化）；`T.dynamic(...)` 声明的是**运行时符号**。参见 u2-l1。

一个本讲要反复用到的关键事实：在 TileKernels 里，`num_sms` **一律作为编译期参数**传入 kernel 构造函数，从不在 `@T.prim_func` 内部运行时读取。这一点决定了 `set_num_sms` 的全部行为。

## 3. 本讲源码地图

本讲涉及三个文件，恰好对应三个最小模块：

| 文件 | 角色 | 本讲解读的重点 |
| --- | --- | --- |
| `tile_kernels/config.py` | 硬件探测与可配置覆盖的唯一入口 | 四个函数的职责、缓存策略、override 机制 |
| `tile_kernels/engram/engram_gate_kernel.py` | engram 门控 kernel（持久化调度范例） | `_choose_num_persistent_blocks` 占用启发式、`num_sms` 如何流成编译期参数、网格如何绑定硬件 |
| `tile_kernels/testing/generator.py` | 测试参数生成器 | `generate_num_sms` 如何生成 SM 扫描列表 |

一句话导航：`config.py` 提供「硬件真相」，`engram_gate_kernel.py` 演示「算子如何消费这份真相」，`testing/generator.py` 提供「测试如何系统化地扰动这份真相」。

## 4. 核心概念与源码讲解

### 4.1 config：硬件探测与可配置覆盖

#### 4.1.1 概念说明

高性能算子要把硬件吃满，第一步是「问清楚硬件有多少资源」。TileKernels 把这件事集中到 `tile_kernels/config.py`，对外提供四个函数，回答两个硬件问题加一个覆盖机制：

- **「这张卡有多少个 SM？」** → `get_device_num_sms()`
- **「每个 SM 有多少共享内存？」** → `get_max_smem_per_sm()`
- **「我想假装卡只有 N 个 SM，做实验/调优，行不行？」** → `set_num_sms(N)` 设置覆盖，`get_num_sms()` 读取「生效中的 SM 数」（有覆盖用覆盖，没覆盖回退到设备真实值）。

为什么要把这两个硬件属性单独抽出来？因为它们是**算子网格与占用启发式的输入**：网格该开多少个 block、每个 SM 能驻留几个 block，都取决于这两个数。把它们收口到一个带缓存的模块里，既避免每个算子各自重复 `torch.cuda.get_device_properties`，也提供了一个统一的「调优旋钮」入口（`set_num_sms`）。

#### 4.1.2 核心流程

读取「生效 SM 数」的优先级如下：

```
get_num_sms()
  ├─ 全局 _num_sms != 0？  → 返回 _num_sms（用户用 set_num_sms 覆盖的值）
  └─ 否则                  → 返回 get_device_num_sms()（设备真实值，带 lru_cache）
```

`set_num_sms(N)` 带一条硬约束：`0 < N <= get_device_num_sms()`——你不能把 SM 数调得比物理上更多，但可以调得更少。设置后，**之后所有调用 `get_num_sms()` 的算子都会读到这个新值**，直到再次调用 `set_num_sms` 改写。

两个硬件探测函数 `get_device_num_sms` / `get_max_smem_per_sm` 都用 `@functools.lru_cache(maxsize=None)` 装饰：硬件属性在一次进程里不会变，缓存避免重复查询开销。而 `get_num_sms` / `set_num_sms` **故意不缓存**——它们操作的是一个可变全局 `_num_sms`，必须每次实时读取，否则 `set_num_sms` 就不生效了。

#### 4.1.3 源码精读

整个文件只有 30 行，逐段看。

**设备 SM 数探测**（带 `lru_cache`）：

[config.py:7-10](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py#L7-L10) —— 从 `torch.cuda.get_device_properties` 读取 `multi_processor_count`（SM 数），结果被 `lru_cache` 缓存：

```python
@functools.lru_cache(maxsize=None)
def get_device_num_sms() -> int:
    prop = torch.cuda.get_device_properties(torch.cuda.current_device())
    return prop.multi_processor_count
```

**可配置覆盖**——先 `set` 再 `get` 的一对函数：

[config.py:13-23](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py#L13-L23) —— `set_num_sms` 改写全局变量 `_num_sms` 并校验上界；`get_num_sms` 在 `_num_sms != 0` 时返回覆盖值，否则回退到设备真实值：

```python
def set_num_sms(num_sms: int) -> None:
    global _num_sms
    assert 0 < num_sms <= get_device_num_sms()
    _num_sms = num_sms

def get_num_sms() -> int:
    global _num_sms
    if _num_sms == 0:
        return get_device_num_sms()
    return _num_sms
```

注意 `assert 0 < num_sms <= get_device_num_sms()`：上限锁死在物理 SM 数，下限要求至少 1。这就是「限制可用 SM」的安全边界——也是本讲综合实践的旋钮。

**每 SM 共享内存探测**（同样带 `lru_cache`）：

[config.py:26-29](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/config.py#L26-L29) —— 读取 `shared_memory_per_multiprocessor`，即每个 SM 可供其上所有驻留 block 共享使用的 SMEM 总量：

```python
@functools.lru_cache(maxsize=None)
def get_max_smem_per_sm() -> int:
    prop = torch.cuda.get_device_properties(torch.cuda.current_device())
    return prop.shared_memory_per_multiprocessor
```

这四个函数被包入口再导出（`tile_kernels/__init__.py`），所以你可以直接 `from tile_kernels.config import set_num_sms` 或 `tile_kernels.get_num_sms()` 使用。

#### 4.1.4 代码实践

**实践目标**：亲手读出你本机的两个硬件数，并验证 `set_num_sms` / `get_num_sms` 的覆盖与回退语义。

**操作步骤**：

1. 编写下面这段「示例代码」（非项目原有代码），在有 GPU + tilelang 的环境里运行：

   ```python
   # 示例代码
   from tile_kernels.config import (
       get_device_num_sms, get_max_smem_per_sm,
       get_num_sms, set_num_sms,
   )

   dev_sms = get_device_num_sms()
   smem    = get_max_smem_per_sm()
   print(f"device SMs = {dev_sms}")
   print(f"max smem per SM = {smem} bytes ({smem/1024:.0f} KiB)")

   # 默认：get_num_sms 回退到设备真实值
   assert get_num_sms() == dev_sms

   # 覆盖：限制为更小的值
   set_num_sms(dev_sms - 20)
   assert get_num_sms() == dev_sms - 20

   # 越界：超过物理 SM 数应抛 AssertionError
   try:
       set_num_sms(dev_sms + 1)
   except AssertionError:
       print("正确：set_num_sms 拒绝了超过物理 SM 数的值")
   ```

2. 把读到的 `dev_sms` 和 `smem` 两个数记下来——4.2 节的手算会直接用到。

**需要观察的现象**：

- `get_num_sms()` 默认等于 `get_device_num_sms()`。
- `set_num_sms` 之后 `get_num_sms()` 立即返回新值（说明它没有缓存）。
- 设大于物理 SM 数的值会被 `assert` 拦截。

**预期结果**：在常见的 SM90/SM100 卡上，`device SMs` 通常是几十到一百多（如 H100 为 132），`max smem per SM` 通常是 200 KiB 量级。**具体数值以你本机读数为准**；若本机无 CUDA 环境，此步标记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `get_device_num_sms` 带 `lru_cache`，而 `get_num_sms` 不带？

**参考答案**：`get_device_num_sms` 查询的是硬件物理属性，一次进程内不会变，缓存可省去重复 `get_device_properties` 调用；`get_num_sms` 读取的是可变全局 `_num_sms`，它的语义就是「实时反映用户最近一次 `set_num_sms`」，一旦缓存就会让覆盖机制失效。

**练习 2**：如果某测试先 `set_num_sms(100)` 跑了一批用例，后续另一个测试不重置就调用 `get_num_sms()`，会读到什么？这会带来什么隐患？

**参考答案**：会读到 `100`，因为 `_num_sms` 是进程级全局状态。隐患是测试之间存在隐式耦合——前一个测试的 SM 限制会「泄漏」到后一个测试。这也是为什么扫描型测试（见 4.3）通常在循环内显式 `set_num_sms`，且把 `device_num_sms` 放在生成列表最后一项以便恢复默认。

---

### 4.2 engram_gate_kernel：持久化 kernel 的占用启发式

#### 4.2.1 概念说明

有了 `config.py` 提供的硬件数，下一步看「算子怎么用它们」。engram 门控前向 kernel 是 TileKernels 里最典型的**持久化 kernel**，它的网格不绑数据规模，而绑硬件 SM 数。这就引出本模块要回答的核心问题：

> 给定 `hidden_size` 和硬件，持久化 block 到底该开多少个？

开少了，SM 闲置、并行度不足；开多了，block 间争抢 SMEM/寄存器、且当 block 数远超 (head,token) 工作量时大量 block 空转。`engram_gate_kernel.py` 用一个纯 Python 的启发式函数 `_choose_num_persistent_blocks` 在**编译期**估算这个数，把「硬件资源 → 网格规模」的映射显式写出来。理解了这个函数，你也就理解了 TileKernels 里绝大多数「硬件感知」算子的调优思路。

承接 u6-l1：那里给出了前向数学（RMSNorm + 融合权重点积 + sigmoid 门控 + 残差）与占用公式的结论；本讲**深挖这条公式是怎么从 `config.py` 一路流进编译产物的**，以及为什么 `set_num_sms` 能改变它。

#### 4.2.2 核心流程

占用估算分两步，全部发生在 kernel 构造函数（`get_engram_gate_fwd_kernel`）里、`@T.prim_func` 之外——也就是**编译期**：

```
1. 选 tile 大小 blk_d：
   从 [1024, 768, 512, 256] 里挑第一个满足
     hidden_size % blk_d == 0  且  hidden_size >= 2 * blk_d
   的值。

2. 估算每 SM 能驻留几个 block，再推持久化块总数：
   smem_bytes        = hidden_size * 2 + blk_d * 4        # x_smem(bf16) + kv 双缓冲(bf16)
   blocks_per_sm     = min( ⌊max_smem_per_sm / smem_bytes⌋ , 16 )   # 16: 寄存器压力上限
   num_persistent_blocks = ⌊num_sms * blocks_per_sm / hc_mult⌋
```

写成数学公式：

\[
\text{smem\_bytes} = 2\,H + 4\,\text{blk\_d}
\]

\[
\text{blocks\_per\_sm} = \min\!\left(\left\lfloor \frac{\text{max\_smem\_per\_sm}}{\text{smem\_bytes}} \right\rfloor,\; 16\right)
\]

\[
\text{num\_persistent\_blocks} = \left\lfloor \frac{\text{num\_sms} \cdot \text{blocks\_per\_sm}}{\text{hc\_mult}} \right\rfloor
\]

要点解读：

- **`smem_bytes` 里为什么是 `hidden_size*2 + blk_d*4`？** `x_smem` 要放下整个 `hidden_size` 的 bf16（每元素 2 字节），`kv_smem` 是 `[2, blk_d]` 的双缓冲（2 份 × `blk_d` × 2 字节 = `blk_d*4`）。这是该 block 的 SMEM 足迹。
- **`blocks_per_sm` 的两个上限**：`max_smem // smem_bytes` 是「SMEM 能塞下几个」；常量 `16` 是「寄存器压力允许几个」的经验上限。两者取 `min` 才是真实可驻留数。
- **`/ hc_mult`**：网格是 `(hc_mult, num_persistent_blocks)`，总 block 数 = `hc_mult × num_persistent_blocks`。除以 `hc_mult` 是为了让**总 block 数**恰好填满 `num_sms × blocks_per_sm` 个「驻留槽位」，即正好铺满一个硬件 wave。
- **`num_sms` 的来源**：它是构造函数的**编译期参数**，由 wrapper 在 Python 侧调用 `get_num_sms()` 传入（见 4.2.3）。所以 `set_num_sms` 改值后，下一次 wrapper 调用会用新值重新估算 `num_persistent_blocks`，从而**重新编译**出一个网格不同的 kernel。

一个本讲的关键结论：因为 `num_sms` 是编译期参数，`set_num_sms` 只影响**性能**（网格规模、并行度），**不影响正确性**——持久化 block 只是把 token 区间 \([0,\text{num\_tokens})\) 切成 `num_persistent_blocks` 段不重不漏地处理，切多切少都覆盖全集。

#### 4.2.3 源码精读

**导入**——engram 只用了 `config` 的两个接口：

[engram_gate_kernel.py:8](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L8) —— 导入 `get_max_smem_per_sm`（占用估算用）和 `get_num_sms`（wrapper 传参用），注意它**不**导入 `set_num_sms`（调优是测试侧的事）：

```python
from tile_kernels.config import get_max_smem_per_sm, get_num_sms
```

**选 tile 大小**：

[engram_gate_kernel.py:35-39](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L35-L39) —— `_choose_blk_d` 按整除性与「至少 2 个 tile」约束，从大到小挑 `blk_d`：

```python
def _choose_blk_d(hidden_size):
    for blk in [1024, 768, 512, 256]:
        if hidden_size % blk == 0 and hidden_size >= 2 * blk:
            return blk
    raise ValueError(f'No valid blk_d for hidden_size={hidden_size}')
```

**占用启发式**——本模块的主角：

[engram_gate_kernel.py:41-46](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L41-L46) —— 注释点明「按 SM 共享内存占用估算，再用 16 封顶寄存器压力」：

```python
def _choose_num_persistent_blocks(hidden_size, blk_d, num_sms, hc_mult):
    """Estimate from SM shared memory occupancy, capped by register pressure."""
    smem_bytes = hidden_size * 2 + blk_d * 4
    blocks_per_sm = min(get_max_smem_per_sm() // smem_bytes, 16)
    return num_sms * blocks_per_sm // hc_mult
```

注意 `get_max_smem_per_sm()` 是在这里直接调用的（带 `lru_cache`，开销可忽略），而 `num_sms` 是作为参数传入——因为它来自可覆盖的 `get_num_sms()`，不能被缓存。

**网格绑定硬件**——`num_persistent_blocks` 进了 `T.Kernel` 的网格维度：

[engram_gate_kernel.py:70](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L70) —— 网格是 `(hc_mult, num_persistent_blocks)`，`num_persistent_blocks` 由上面的启发式决定，与 `num_tokens` 无关：

```python
with T.Kernel(hc_mult, num_persistent_blocks, threads=threads) as (pid_h, pid_b):
```

**token 分片**——每个持久化 block 处理哪一段 token：

[engram_gate_kernel.py:86-90](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L86-L90) —— `per_block = ceildiv(num_tokens, num_persistent_blocks)`，块 `pid_b` 负责区间 \([t\_start, t\_end)\)，`Serial` 循环逐 token 推进，不重不漏：

```python
per_block = T.ceildiv(num_tokens, num_persistent_blocks)
t_start = T.min(per_block * pid_b, num_tokens)
t_end = T.min(per_block * (pid_b + 1), num_tokens)

for i_s in T.Serial(t_start, t_end):
```

`T.min(..., num_tokens)` 是关键护栏：当 `num_persistent_blocks > num_tokens` 时，尾部 block 的 `t_start` 会被钳到 `num_tokens`，于是 `t_start == t_end`，循环体不执行——这些 block **空转**。这正是「SM 加到超过工作量就不再有收益」的物理解释。

**num_sms 如何流成编译期参数**——wrapper 在 Python 侧读 `get_num_sms()` 并传入构造函数：

[engram_gate_kernel.py:502](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L502) —— 前向 wrapper 把 `get_num_sms()` 作为 `num_sms` 实参传给 jit 装饰的构造函数：

```python
kernel = get_engram_gate_fwd_kernel(hidden_size, eps, scalar, k_stride_s, k_stride_h, v_stride_s, get_num_sms(), clamp_value, hc_mult, save_for_backward)
```

因为 `get_engram_gate_fwd_kernel` 被 `@tilelang.jit` 装饰（[engram_gate_kernel.py:11-16](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L11-L16)），编译产物按**参数元组**缓存。所以：

- 不同的 `get_num_sms()` 返回值 → 不同的参数元组 → JIT 缓存未命中 → **重新编译**出一个网格不同的 kernel。
- 同一个 `num_sms` 第二次调用 → 缓存命中 → 直接复用已编译产物。

这就是 `set_num_sms`「改性能不改正确性」、且「每个新值都要付一次编译代价」的根因。

> 旁注（反向 kernel 的差异）：反向 kernel 的持久化块数更简单——`num_persistent_blocks` 直接等于 `num_sms`（见 [engram_gate_kernel.py:557](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L557) 传 `get_num_sms()` 作 `num_persistent_blocks`、网格 [engram_gate_kernel.py:276](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L276) 为 `T.Kernel(num_persistent_blocks, ...)`），没有 `× blocks_per_sm ÷ hc_mult` 这层放大。原因是反向每 block 用 8 个 warp（256 线程，见 `threads = warp_size * num_warps`），单个 block 更「重」，每个 SM 驻留 1 个 block 即可；且反向的 `grad_w_partial` 显式分配成 `(get_num_sms(), hc_mult, hidden_size)`（[engram_gate_kernel.py:564](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L564)），由后续 `grad_w_reduce` 二次归约（参见 u6-l2）。前后向的启发式不同，但都把 `num_sms` 当编译期参数。

#### 4.2.4 代码实践

**实践目标**：用 4.1.4 读到的本机硬件数，手算给定 `hidden_size` 下的 `num_persistent_blocks`，并验证「`set_num_sms` 线性缩放持久化块数」这一结论。这一步**不需要跑 GPU**，纯算术即可验证对公式的理解。

**操作步骤**：

1. 设你本机读到的硬件数为 `dev_sms` 个 SM、每 SM `max_smem` 字节共享内存（若无 GPU，可借用下方 H100 示例数代入）。对 `hc_mult = 4`、`hidden_size ∈ {4096, 7168}` 填表：

   | hidden_size | blk_d | smem_bytes = 2H+4·blk_d | ⌊max_smem/smem_bytes⌋ | blocks_per_sm = min(·,16) | num_persistent_blocks = ⌊num_sms·blocks_per_sm/4⌋ |
   | ---: | ---: | ---: | ---: | ---: | ---: |
   | 4096 | ? | ? | ? | ? | ? |
   | 7168 | ? | ? | ? | ? | ? |

2. **以一张常见的 H100（SM90）为例**（`multi_processor_count = 132`、`shared_memory_per_multiprocessor = 232448` 字节，**以你本机 `get_max_smem_per_sm()` 读数为准**），手算如下（示例演算，便于你对照）：
   - `hidden_size = 4096`：`_choose_blk_d` 取 `1024`（4096%1024=0 且 4096≥2048）；
     - `smem_bytes = 4096×2 + 1024×4 = 8192 + 4096 = 12288`
     - `⌊232448 / 12288⌋ = 18` → `blocks_per_sm = min(18, 16) = 16`
     - `num_persistent_blocks = ⌊132×16/4⌋ = ⌊2112/4⌋ = 528`
   - `hidden_size = 7168`：`blk_d = 1024`（7168=7×1024）；
     - `smem_bytes = 7168×2 + 1024×4 = 14336 + 4096 = 18432`
     - `⌊232448 / 18432⌋ = 12` → `blocks_per_sm = min(12, 16) = 12`
     - `num_persistent_blocks = ⌊132×12/4⌋ = ⌊1584/4⌋ = 396`
3. 再用 `set_num_sms(112)`（即 `132 - 20`）重算 `num_persistent_blocks`：
   - 4096：`blocks_per_sm` 仍为 16（与 `num_sms` 无关），`num_persistent_blocks = ⌊112×16/4⌋ = 448`
   - 7168：`num_persistent_blocks = ⌊112×12/4⌋ = 336`

**需要观察的现象**：

- `blocks_per_sm` 只取决于 `max_smem` 与 `smem_bytes`，**与 `num_sms` 无关**；`num_sms` 只线性地缩放最终的 `num_persistent_blocks`。
- 当 `num_persistent_blocks × hc_mult`（即网格总 block 数）显著大于 `num_tokens`（的若干倍）时，进一步增大 `num_sms` 不再提升并行度——因为多出来的 block 会落到 `t_start == t_end` 空转。例如 `hidden=4096`、`num_sms=132` 时网格有 `4×528=2112` 个 block，而 `num_tokens=4001` 时只有 `4×4001≈16000` 个 (head,token) 工作单元，平均每块约 7.6 个；若 `num_tokens` 很小，大量 block 必然空转。

**预期结果**：你手算的 `num_persistent_blocks` 应与上表演算一致（允许因 `max_smem` 读数不同而不同）。注意源码第 34 行的注释 `Performance only tuned for hidden_size in {4096, 7168}`——这两个尺寸之外的值（如 2048/3072/6144）公式仍会给出一个数，但未做性能调优。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `blocks_per_sm` 要和常量 `16` 取 `min`？如果去掉这个 `min`，只看 SMEM，会发生什么？

**参考答案**：SMEM 只是占用率的一个维度，另一个是寄存器。单个 block 的寄存器用量随 `blk_d`、线程数增长，即使 SMEM 还塞得下，寄存器压力也可能让单个 SM 驻留不下太多 block；`16` 是经验性的寄存器压力上限。去掉 `min` 会让启发式高估可驻留 block 数，导致网格过大、实际占用率反而下降或 launch 失败。

**练习 2**：`set_num_sms` 把 `num_sms` 减半后，`num_persistent_blocks` 一定减半吗？什么情况下不会？

**参考答案**：在 `blocks_per_sm` 不变（一般不变，因为它与 `num_sms` 无关）的前提下，`num_persistent_blocks = ⌊num_sms·blocks_per_sm/hc_mult⌋` 基本随 `num_sms` 线性变化，减半 `num_sms` 大致减半结果。但整除向下取整会引入误差：当 `num_sms·blocks_per_sm` 不是 `hc_mult` 的整数倍时，减半前后的取整可能让比例略偏离 2:1。

**练习 3**：engram 前向 wrapper 调用 `get_num_sms()` 的时机是「每次 wrapper 调用」还是「进程启动一次」？这对测试有什么影响？

**参考答案**：是「每次 wrapper 调用」（见 [engram_gate_kernel.py:502](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/engram/engram_gate_kernel.py#L502)）。因此测试里 `set_num_sms(N)` 后立即调用 `engram_gate_fwd(...)` 就会用新值，并触发一次针对该 `N` 的重新编译。影响是：扫描多个 `N` 会付多次编译代价，所以测试扫描列表（见 4.3）只取两三个代表性值，而非密集扫描。

---

### 4.3 testing/generator：SM 扫描参数生成

#### 4.3.1 概念说明

「`set_num_sms` 能改性能不改正确性」是一个可被自动化验证的不变量。TileKernels 在 `testing/generator.py` 里提供了一个专门的参数生成器 `generate_num_sms()`，让正确性测试在若干个代表性 SM 数下都跑一遍，确保算子在「SM 受限」时仍然数值正确。这个模块回答的是**测试侧**的问题：扰动硬件并行度时，该取哪几个值才有代表性又不至于把测试时间拖垮？

它和前两个模块构成完整闭环：`config.py` 提供旋钮 → 算子（如 engram）把旋钮当编译期参数消费 → `generate_num_sms()` 在测试里系统化地拧这个旋钮。

#### 4.3.2 核心流程

`generate_num_sms()` 的取值策略：

```
base_list = [device_num_sms - 20, device_num_sms]   # 「受限」与「满载」两个代表点
extra_list = [1]                                     # FULL 模式追加极端点：单 SM

TK_FULL_TEST 开启？
  ├─ 是 → 返回 [1, device_num_sms-20, device_num_sms]
  └─ 否 → 返回 [device_num_sms-20, device_num_sms]
```

设计要点：

- **`device_num_sms - 20`**：模拟「SM 被占用一部分」的真实部署场景（同一张卡上还有别的任务在跑），验证算子在非满载下仍正确。`-20` 是一个固定偏移而非比例，所以对小卡（如 48 SM）也能给出一个明显不同的值。
- **`device_num_sms`**：满载默认值，放在列表**最后一项**（注释明说 `for convenience of testing`）——这样测试循环跑完最后一个值后，全局 `_num_sms` 恰好停在默认满载，避免「状态泄漏」影响后续测试（呼应 4.1.5 练习 2）。
- **`[1]` 仅 FULL 档**：单 SM 是极端边界（并行度退化为 1），只在你显式 `TK_FULL_TEST=1` 时才跑，日常正确性测试不开。

注意 `generate_num_sms()` 调用的是 `get_device_num_sms()`（**物理真实值**），而不是 `get_num_sms()`（可能已被 `set_num_sms` 污染的值）——这样扫描基线永远以硬件为准，不受测试间状态影响。

#### 4.3.3 源码精读

**生成器定义**：

[generator.py:26-32](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/generator.py#L26-L32) —— 以 `device_num_sms` 为基线，默认给「受限 + 满载」两点，FULL 模式追加单 SM 极端点，并把 `device_num_sms` 排在末尾：

```python
def generate_num_sms() -> list[int]:
    device_num_sms = get_device_num_sms()
    do_full_test = os.getenv('TK_FULL_TEST') in ['1', 'true', 'True']
    extra_list = [1, ]
    base_list = [device_num_sms - 20, device_num_sms, ]
    # Ensure `device_num_sms` is the last one in the list for convenience of testing
    return extra_list + base_list if do_full_test else base_list
```

它从 `config` 导入的是 `get_device_num_sms`（[generator.py:7](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/generator.py#L7)），刻意用物理值而非可覆盖值。

**测试侧的标准用法**——以 `tests/moe/test_group_count.py` 为范例（group_count 这类 histogram 算子对 SM 数敏感，是扫描验证的好样板）：

[test_group_count.py:32-35](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_group_count.py#L32-L35) —— 对每个 SM 数 `set_num_sms` 后重跑，断言与 torch 参考仍逐位相等：

```python
for num_sms in generate_num_sms():
    set_num_sms(num_sms)
    count = tile_kernels.moe.group_count(topk_idx, num_experts)
    assert_equal(count, count_ref)
```

这就是「`set_num_sms` 不改正确性」的自动化证明：每个 `num_sms` 下都和同一个 `count_ref` 对拍。同一模式也出现在 `test_get_fused_mapping.py`、`test_aux_fi.py`、`test_inplace_unique_group_indices.py`、`test_swiglu_forward_and_per_token_cast.py` 等多个对 SM 数敏感的算子测试里。

> 小贴士：engram 的正确性测试（`tests/engram/test_engram_gate_fwd.py`）目前**不**扫描 `num_sms`，因为它的 `num_persistent_blocks` 只切分 token 区间、不改变数值；engram 对 SM 数的依赖体现在**性能**而非正确性上，所以把它放进 benchmark 扫描（见综合实践）更有意义。

#### 4.3.4 代码实践

**实践目标**：读懂「扫描 SM 数验证正确性」这一测试范式，并解释 `generate_num_sms` 的列表顺序为何如此设计。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 打开 [tests/moe/test_group_count.py:24-35](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/moe/test_group_count.py#L24-L35)，跟读 `test_group_count`：先用 torch 算一次参考 `count_ref`，再在 `for num_sms in generate_num_sms():` 循环里反复 `set_num_sms` + 调 kernel + `assert_equal`。
2. 回答：循环结束时全局 `_num_sms` 等于多少？为什么这能让下一个测试用例「不受影响」？
3. （可选，需 GPU）在本机跑：`pytest tests/moe/test_group_count.py -v`，再 `TK_FULL_TEST=1 pytest tests/moe/test_group_count.py -v`，对比用例数的变化（FULL 档应多出一组 `num_sms=1` 的扫描）。

**需要观察的现象**：

- 每个正确性用例至少跑 2 次（`device-20` 与 `device`），FULL 档跑 3 次（多了 `1`）。
- 不同 `num_sms` 下，kernel 输出与 torch 参考始终 `assert_equal`（逐位相等）。

**预期结果**：循环结束时 `_num_sms == device_num_sms`（因为 `device_num_sms` 是列表末项），下一个测试默认就在满载下运行。FULL 档比默认档多覆盖单 SM 边界。若本机无 GPU，步骤 3 标记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `generate_num_sms` 调 `get_device_num_sms()` 而不是 `get_num_sms()`？

**参考答案**：`get_num_sms()` 可能已被前一个测试的 `set_num_sms` 改写，读它会得到污染的基线；`get_device_num_sms()` 永远返回物理真实值（且带 `lru_cache`），保证扫描基线稳定、可复现，不受测试执行顺序影响。

**练习 2**：把 `base_list` 改成 `[device_num_sms, device_num_sms - 20]`（交换顺序）会引入什么问题？

**参考答案**：循环结束时 `_num_sms` 会停在 `device_num_sms - 20` 而非满载值，使「SM 受限」状态泄漏到后续测试，破坏隔离性。这正是注释 `Ensure device_num_sms is the last one` 的用意。

## 5. 综合实践

把本讲三个最小模块串起来，设计一个**「engram 前向的 SM 敏感性微基准」**。目标：亲手验证「`set_num_sms` 只改性能不改正确性」，并观察带宽随 SM 数的变化曲线，判断 engram 前向属于算力受限还是带宽受限。

**任务**：编写一个「示例代码」脚本（非项目原有文件，建议放在临时目录，不要写进仓库），完成以下三件事：

1. **手算预估**：用 4.2.4 的公式，对 `hidden_size = 4096`、`hc_mult = 4`、你本机的 `dev_sms` 与 `max_smem`，算出默认 `num_persistent_blocks`。
2. **正确性扫描**：仿照 `test_group_count.py` 的范式，对 `generate_num_sms()` 的每个值 `set_num_sms` 后调用 `engram_gate_fwd`，与 `engram_gate_ref` 用 `calc_diff` 对拍，断言误差小于 `2e-10`（阈值取自 `tests/engram/test_engram_gate_fwd.py`）。
3. **性能扫描**：对同一组 `num_sms`，用 `benchmark_timer` 测延迟、用 `count_bytes` 算有效带宽 `bandwidth_gbs = num_bytes / t_us / 1e3`，打印一张 `num_sms → time_us → bandwidth_gbs` 表。

参考骨架（示例代码）：

```python
# 示例代码（需 GPU + tilelang 环境）
import torch
from tile_kernels.engram import engram_gate_fwd
from tile_kernels.torch.engram import engram_gate_ref
from tile_kernels.config import set_num_sms, get_device_num_sms, get_max_smem_per_sm
from tile_kernels.testing.generator import generate_num_sms
from tile_kernels.testing.numeric import calc_diff, count_bytes
# 假设你已从项目的 benchmark 插件拿到 benchmark_timer；否则可用 torch.cuda Event 自行计时

num_tokens, hc, hidden, eps, clamp_value = 4001, 4, 4096, 1e-20, 1e-6
x = torch.randn(num_tokens, hc, hidden, dtype=torch.bfloat16, device='cuda')
k = torch.randn(num_tokens, hc, hidden, dtype=torch.bfloat16, device='cuda')
v = torch.randn(num_tokens, hidden, dtype=torch.bfloat16, device='cuda')
wh = torch.randn(hc, hidden, dtype=torch.bfloat16, device='cuda')
we = torch.randn(hc, hidden, dtype=torch.bfloat16, device='cuda')
weight_fused = wh.float() * we.float()

out_ref, *_ = engram_gate_ref(x, k, v, wh, we, clamp_value, eps, save_for_backward=True)

for num_sms in generate_num_sms():
    set_num_sms(num_sms)
    out, *_ = engram_gate_fwd(x, k, v, weight_fused, eps, clamp_value, save_for_backward=True)
    assert calc_diff(out, out_ref) < 2e-10   # 正确性不随 SM 数变化
    # t_us = benchmark_timer(lambda: engram_gate_fwd(x, k, v, weight_fused, eps, clamp_value, save_for_backward=False)[0])
    # num_bytes = count_bytes(x, k, v, weight_fused, out)
    # print(f"num_sms={num_sms:4d}  t_us={t_us:8.2f}  bw={num_bytes/t_us/1e3:6.1f} GB/s")
```

**需要观察与思考的现象**：

1. **正确性列**：所有 `num_sms` 下 `calc_diff` 都应通过——证明 `set_num_sms` 不改正确性。
2. **延迟列**：`num_sms` 从 `device-20` 升到 `device`，延迟应下降（并行度提高）；但若 `num_tokens` 很小，进一步增大 SM 数收益会变缓（block 空转，见 4.2.4）。
3. **带宽列**：把测得的有效带宽与你该卡的显存峰值带宽对比（如 H100 SXM 约 3.35 TB/s）。若有效带宽接近峰值的较高比例，说明 engram 前向是**带宽受限**算子——这与它「读 x/k/v、写 out、跨 token 复用 weight_fused」的访存特征一致。

**预期结果**：正确性扫描全部通过；性能扫描呈现「SM 数↑ → 延迟↓，但边际收益递减」的曲线。具体延迟与带宽数值**待本地验证**（取决于硬件与 `benchmark_timer` 的 CUPTI 计时）。

## 6. 本讲小结

- `tile_kernels/config.py` 是硬件感知的唯一入口：`get_device_num_sms`/`get_max_smem_per_sm` 带 `lru_cache` 探测物理属性，`get_num_sms`/`set_num_sms` 不带缓存以支持运行时覆盖，`set_num_sms` 用 `assert` 锁定上界为物理 SM 数。
- 在 TileKernels 里，`num_sms` **一律是编译期参数**（在 wrapper 的 Python 侧经 `get_num_sms()` 读出后传入 jit 装饰的构造函数），从不在 `prim_func` 内运行时读取；因此 `set_num_sms` 会触发针对新值的重新编译。
- engram 前向的占用启发式 `_choose_num_persistent_blocks` 把硬件资源映射成网格规模：\(\text{num\_persistent\_blocks}=\lfloor \text{num\_sms}\cdot\min(\lfloor\text{max\_smem}/(2H+4\,\text{blk\_d})\rfloor,16)/\text{hc\_mult}\rfloor\)，其中 `blocks_per_sm` 与 `num_sms` 无关，故 `set_num_sms` 线性缩放持久化块数。
- 持久化网格 `(hc_mult, num_persistent_blocks)` 绑硬件而非数据；token 由 `per_block = ceildiv(num_tokens, num_persistent_blocks)` 切片不重不漏，`T.min(..., num_tokens)` 护栏让多出的 block 空转——这是「SM 加到超过工作量就无收益」的根源。
- `set_num_sms` 只改性能不改正确性，这一不变量由 `generate_num_sms()` + `set_num_sms` 的扫描对拍范式在多个测试里自动化验证；`generate_num_sms` 用物理 `get_device_num_sms()` 作基线、并把满载值排在列表末尾以防状态泄漏。

## 7. 下一步学习建议

- **下一讲 u10-l2（TMA、向量化、布局与 pass_configs）**：本讲聚焦「SM/SMEM 感知」这一类硬件调优，下一讲转向另一类——访存向量化（`get_best_vectorize_size`）、非连续输入（`T.StridedTensor`）与编译开关（`pass_configs` 如 `TL_DISABLE_WARP_SPECIALIZED`，engram 前向正是用它关闭了 warp specialization）。两者合起来覆盖了 TileKernels 的主要调优旋钮。
- **延伸阅读**：对照 `tests/moe/test_group_count.py`、`test_get_fused_mapping.py` 等多个扫描 SM 数的测试，体会「哪些算子对 SM 数敏感（histogram/scatter 类，需扫描验证正确性）、哪些不敏感（如 engram 前向，只影响性能）」。
- **回顾 u6-l1/u6-l2**：把本讲的占用启发式放回 engram 前向数学与反向 `grad_w_reduce` 的上下文里，理解「为什么前向网格放大 `blocks_per_sm` 倍、反向却不放大」的设计取舍。
- **动手实验**：在综合实践基础上，把 `num_tokens` 从几百扫到几万，画出「延迟 vs num_tokens」与「带宽 vs num_sms」两张图，定位 engram 前向在什么规模下从「launch/占用受限」转入「带宽受限」。

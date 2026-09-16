# 扩展与二次开发

## 1. 本讲目标

本讲是学习手册的最后一讲，把前五个单元积累的源码认知收敛成一份「二次开发手册」。学完后你应该能够：

1. **定位扩展新权重 dtype 时需要改动的文件清单**：理解 `_ELEM_TYPES` 类型注册表如何把一个 `torch.dtype` 一路传播到编译期常量、smem 预算与指针类型，从而能独立完成「给 `prefetch_weight` 增加 float16 支持」这样的任务。
2. **理解 B、num_sms、token_padding 等调优参数的影响面**：知道每个旋钮改的是哪块显存、哪个内核的启动几何、哪条容量公式，调参时能预判代价。
3. **掌握 Buffer 生命周期与 destroy 的正确顺序**：能解释「先同步通信流、再同步设备、再集合屏障、最后按视图先于属主的顺序解除 VMM 映射」这条协议为什么不能颠倒。
4. **评估新设备适配的真实工作量**：把仓库按「CUDA 驱动 API 层 / PTX 指令层 / PyTorch 设备假设层」切成三块移植面，知道 Zhenwu PPU 支持目前仅是 README 的一行承诺（under review，待确认）。

## 2. 前置知识

本讲默认你已完成 u6-l3（训练全链路）与 u5-l1/u5-l2（预取内核），这里只做简要回顾：

- **权重契约**：每个专家投影（gate/up/down）持有**一根连续的** `[E+B, H, H']` 对称内存张量；前 `E` 行是全组专家（物理上是各 home rank 的参数显存），后 `B` 行是本地预取槽。`prefetch_weight` 按 `plan.experts_to_copy` 把远程专家权重搬进预取槽。
- **预取内核是「字节搬运工」**：`PrefetchKernel` 用 2D TMA 整块搬运数据，**不解释元素语义**——bf16、打包的 MXFP4（uint8）、重排后的 scale（uint8）走的是同一条代码路径。这正是本讲 4.1 节「加一个 dtype 只需要改一张表」的设计基础。
- **两层代码结构**（u1-l2）：`moonep/` 是 Python 内核层，用 CuTe DSL 编写、首次调用时 JIT 编译；`csrc/` 预编译成 `moonep._C` 扩展，封装 CUDA 驱动 API（VMM、fabric 句柄、NVSwitch 组播）。
- **CuTe DSL 三层模型**（u4-l1）：`@cute.kernel` 设备代码 + `@cute.jit` 宿主入口 + `cute.compile` 按形特化；`const_expr` 把形状烧成编译期常量；`_get_compiled` 用 `lru_cache` 缓存 cubin。
- **CUDA C++ 的 half 宏**：`__CUDA_NO_HALF_OPERATORS__` 一类宏会禁用 CUDA 头文件中 `__half` 的隐式转换与运算符，目的是让 C++ 层的 dtype 混用变成编译错误而不是静默精度事故。本讲 4.3 会看到 MoonEP 在 `setup.py` 里开了四个这类开关。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| `moonep/prefetch.py` | 权重预取内核 | `_ELEM_TYPES` 类型注册表；dtype 如何传播到 smem/指针/cubin 缓存 |
| `moonep/api.py` | Buffer 门面 | 构造参数与调优旋钮；`destroy()`/`__del__` 生命周期协议 |
| `setup.py` | 构建入口 | nvcc/cxx 编译开关、`libraries=["cuda"]`、DSL 版本钉死 |
| `csrc/bindings.cu` | pybind11 绑定 | 11 个导出符号 = 新设备移植的最小 C++ 面 |
| `README.md` | 项目契约 | B 的取值规则、Supported Devices（PPU 待确认） |
| `tests/test_prefetch.py` | 预取测试 | CASES 参数化如何让新 dtype 的测试成本接近零 |
| `benchmarks/bench_prefetch.py` | 预取基准 | `_DT_LABEL` 是 dtype 扩展的可选同步点 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**类型注册表**（4.1）、**Buffer 生命周期**（4.2）、**构建配置与新设备移植面**（4.3）。

### 4.1 类型注册表：`_ELEM_TYPES` 与权重侧 dtype 扩展点

#### 4.1.1 概念说明

MoonEP 对「数据是什么类型」分三条通路管理，**扩展难度天差地别**：

| 通路 | dtype 约束 | 扩展方式 |
| --- | --- | --- |
| hidden 通路（dispatch/combine 的 token 数据） | 硬编码 bf16 | 改 `hidden_buf` 分配 + 多个内核，大工程 |
| 梯度通路（reduce_grad） | 硬编码 fp32 | 改梯度缓冲 + 内核累加精度，中工程 |
| **权重通路（prefetch_weight）** | **注册表 `_ELEM_TYPES` 白名单** | **改一张字典，一处** |

权重通路之所以是「设计好的扩展点」：预取内核只做字节级搬运，元素类型只影响三件事——smem 张量的 CuTe 类型、gmem 指针的编译期类型、以及按字节计算的 smem 预算与 mbarrier 事务计数。这三件事全部由注册表的第二元（字节数）和第一元（CuTe 类型）参数化，代码里没有任何 `if dtype == bfloat16` 的分支。

#### 4.1.2 核心流程

一个 `torch.dtype` 从用户张量到 GPU 内核的传播路径：

```text
用户传入 fp16 权重 [E+B, H, H']
        │
        ▼
Buffer.prefetch_weight:  assert w.dtype in _ELEM_TYPES        (api.py 复用同一注册表)
        │
        ▼
launch_prefetch:         assert dtype in _ELEM_TYPES          (入口白名单)
        │
        ▼
_ELEM_TYPES[torch_dtype] → (elem_ty, elem_bytes)
        │
        ├─► _get_compiled(..., torch_dtype)   ← dtype 是 lru_cache 键的一部分
        │       │
        │       ├─► PrefetchKernel(elem_ty, elem_bytes)
        │       │       ├─► _smem_bytes(stages): tile_bytes = 128·128·elem_bytes
        │       │       └─► _pick_stages: 在 smem 预算内从 6 降到 2 选流水线深度
        │       │
        │       └─► make_ptr(elem_ty, ...)    ← 编译期指针类型必须与张量 dtype 匹配
        │
        └─► 运行期: make_ptr(elem_ty, data_ptr) → compiled(src, dst, experts, stream)
```

设备内核内部，`elem_bytes` 进一步变成编译期常量 `TILE_BYTES`，直接充当 TMA 异步流水线的事务计数（expect_tx）：

\[ \text{TILE\_BYTES} = 128 \times 128 \times b, \qquad b \in \{1, 2\}\ (\text{现状}) \]

smem 总预算公式（`_smem_bytes`，`s` 为流水线级数、`B` 为槽数）：

\[ \text{smem}(s) = \mathrm{align}_{128}\!\left(s \cdot 128 \cdot 128 \cdot b\right) + \mathrm{align}_{16}\!\left(16 s\right) + \mathrm{align}_{16}\!\left(8 B\right) + 256 \]

其中第二项是 `stages` 个 mbarrier（`Int64` × 2/stage），第三项是专家/槽位压缩表（`Int32` × B × 2），尾项 256 是杂项余量。

#### 4.1.3 源码精读

**注册表本体**。三个条目，键是 `torch.dtype`，值是（CuTe 类型, 每元素字节数）二元组：

- [moonep/prefetch.py:L32-L36](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L32-L36) — `_ELEM_TYPES` 定义：bf16→`(BFloat16, 2)`、int8→`(Int8, 1)`、uint8→`(Uint8, 1)`。uint8 一个条目同时服务 MXFP4 打包权重与 ue8m0 scale，因为内核不区分语义。注意顶部的导入行 [moonep/prefetch.py:L28](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L28) 只导入了 `BFloat16, Int8, Int64, Uint8`——**加 float16 时这里必须同步加 `Float16`**。

**构造参数化**。`PrefetchKernel` 把类型信息存为实例属性：

- [moonep/prefetch.py:L48-L72](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L48-L72) — `elem_ty=BFloat16, elem_bytes=2` 是带默认值的构造参数；`stages` 由 `_pick_stages` 按预算选定，选不出（返回 0）时抛出含 dtype 字节数的 `RuntimeError`。
- [moonep/prefetch.py:L74-L90](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L74-L90) — `_smem_bytes` 与 `_pick_stages`：主项 `tile_bytes = M_BLOCK * N_BLOCK * elem_bytes`，候选级数从大到小 `(6, 5, 4, 3, 2)` 取第一个放得下的。

**设备侧的三个消费点**：

- [moonep/prefetch.py:L160-L171](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L160-L171) — `TILE_BYTES = const_expr(TILE_ELEMS * self.elem_bytes)`：字节宽度烧成编译期常量。
- [moonep/prefetch.py:L177-L187](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L177-L187) — smem 中的 stage 张量以 `self.elem_ty` 分配（128 字节对齐）：元素类型决定 smem 视图如何被 TMA 解释。
- [moonep/prefetch.py:L208-L214](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L208-L214) — `PipelineTmaAsync.create(..., tx_count=TILE_BYTES)`：mbarrier 的事务计数按**字节**声明，dtype 变宽时每个 stage 期待的字节数随之变大——这就是「`_smem_bytes` 的 `elem_bytes` 变化」的实质。

**编译缓存把 dtype 当键**：

- [moonep/prefetch.py:L299-L332](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L299-L332) — `_get_compiled` 的 `lru_cache` 键包含 `torch_dtype`；第 310 行 `smem_budget = optin − 1024` 预留 1 KiB 余量；第 322 行用 `make_ptr(elem_ty, 0, ...)` 造一个哑指针参与 `cute.compile`，让 cubin 针对该元素类型特化。**新 dtype 由此自动获得独立缓存的 cubin，与已有 dtype 互不干扰。**
- [moonep/prefetch.py:L294-L296](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L294-L296) — smem 预算取自 `torch.cuda.get_device_properties(device_index).shared_memory_per_block_optin`，同样被 `lru_cache` 按设备缓存。

**入口白名单与运行期指针**：

- [moonep/prefetch.py:L378-L387](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L378-L387) — `assert dtype in _ELEM_TYPES`（报错信息直接列出注册表内容），并要求 `prefetch_buffers.dtype == dtype`：源与目标必须同 dtype。
- [moonep/prefetch.py:L411-L434](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L411-L434) — 运行期再次从 `_ELEM_TYPES[dtype]` 取 `elem_ty` 构造 `make_ptr(elem_ty, data_ptr, ...)`，指针类型必须与编译期一致，否则调用已特化 cubin 时类型错位。
- [moonep/api.py:L899-L907](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L899-L907) — `Buffer.prefetch_weight` 里的 `assert w.dtype in _ELEM_TYPES` **复用同一张注册表**（见 [moonep/api.py:L71](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L71) 的导入），所以扩充字典后这条断言自动放行，无需改动。

**对照组：另两条通路为什么难扩**：

- [moonep/api.py:L361-L364](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L361-L364) — `hidden_buf` 在 `_create_context` 里写死 `torch.bfloat16`；[moonep/api.py:L1001-L1004](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L1001-L1004) — `combine` 断言 `hidden_nvsh.dtype == torch.bfloat16`。hidden 通路换 dtype 要动对称内存分配、dispatch/combine/prologue/epilogue 四个内核的载荷路径。
- [moonep/api.py:L204-L208](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L204-L208) — 梯度通路断言 fp32：预取槽梯度的归约以 fp32 累加为正确性前提（u5-l3）。

#### 4.1.4 代码实践：注册表 dry-run（纯 Python，无需 GPU）

**实践目标**：不改任何源码，验证你对 dtype→smem 传播路径的理解——特别是「float16 与 bfloat16 同为 2 字节，所以加 fp16 后 smem 预算、级数选择、事务计数**数值上全部不变**，变化的只是类型标签」。

**操作步骤**（以下为示例代码，可在任何有 Python 的机器运行，不依赖 cutlass/GPU）：

```python
# smem_dryrun.py —— 复现 PrefetchKernel._smem_bytes / _pick_stages 的数值行为
def round_up(n, a):
    return (n + a - 1) // a * a

def smem_bytes(stages, elem_bytes, B):
    tile_bytes = 128 * 128 * elem_bytes          # M_BLOCK=N_BLOCK=128
    return (round_up(stages * tile_bytes, 128)
            + round_up(stages * 2 * 8, 16)       # mbarrier: Int64 x 2/stage
            + round_up(2 * B * 4, 16)            # exp_tab/slot_tab: Int32 x B
            + 256)

def pick_stages(smem_budget, elem_bytes, B):
    for s in (6, 5, 4, 3, 2):
        if smem_bytes(s, elem_bytes, B) <= smem_budget:
            return s
    return 0

B = 32
optin = 232448            # 示例：SM90 档卡 shared_memory_per_block_optin=227 KiB
budget = optin - 1024     # _get_compiled 里的 -1024 余量
for name, eb in [("uint8/int8", 1), ("bf16", 2), ("fp16(新)", 2), ("假设的4字节", 4)]:
    print(f"{name:10s} elem_bytes={eb} -> stages={pick_stages(budget, eb, B)}, "
          f"smem@picked={smem_bytes(pick_stages(budget, eb, B), eb, B)}")
```

**需要观察的现象**：

1. bf16 与 fp16 两行输出**完全一致**（`stages` 与 smem 字节数都相同）——因为注册表里二者共享 `(·, 2)`。
2. 假设的 4 字节类型会掉到更低的 `stages` 档——这就是「elem_bytes 变化影响 `_smem_bytes`」的可见效果。
3. 把 `B` 从 32 改成 4，主项不变、压缩表项变小，级数不受影响——确认 B 不是 smem 的主导项。

**预期结果**：在示例预算下应看到 1 字节与 2 字节类型都选到最高档 6，4 字节类型选到 3 档左右（具体数值取决于你代入的 `optin`）。本讲义撰写环境无 GPU，`optin` 取的是文档标称值，真实值请以 `_max_smem_per_block_optin(device_index)` 实测为准——**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `torch.float32`（4 字节）也加进 `_ELEM_TYPES`，除了字典本身，还有哪两处「数值」会自动跟着变？

**答案**：`_smem_bytes` 的主项（`128·128·4 = 65536` 字节/tile，可能使 `_pick_stages` 掉档）与 `PipelineTmaAsync` 的 `tx_count`（= `TILE_BYTES`，mbarrier 期待的字节数翻倍）。这两处都由 `elem_bytes` 参数化，无需手改代码，但掉档意味着预取吞吐变化，需要重新基准测试。

**练习 2**：为什么 `launch_prefetch` 的断言报错信息要写成 `sorted(str(d) for d in _ELEM_TYPES)`（遍历注册表）而不是硬编码 `"bf16/int8/uint8"`？

**答案**：注册表是唯一的 dtype 真相源。硬编码字符串会在下次扩充注册表时与白名单脱节（报错说支持 X、断言却拒绝 X）；遍历注册表让报错信息随白名单自动更新，是「单一事实源」原则的最小体现。

**练习 3**：用户把 fp16 权重传给 `prefetch_weight`，但预取槽张量是 bf16，会在哪一行、以什么方式失败？

**答案**：在 [moonep/prefetch.py:L385-L387](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L385-L387) 的 `assert prefetch_buffers.dtype == dtype` 处触发 `AssertionError`（消息为 "prefetch_buffers must be contiguous [B, H, H'] with dtype ..."）。更早的 `Buffer.prefetch_weight` 只校验权重本身在白名单内（api.py L903-L905），不比对槽位 dtype，所以拦截发生在内核封装层。

### 4.2 Buffer 生命周期与调优参数的影响面

#### 4.2.1 概念说明

`Buffer` 是一组**进程级昂贵资源**的属主：两个跨 rank 对称内存区（`hidden_buf`、`meta_buf`，后者还绑定 NVSwitch 组播对象）、十来个本地 scratch 张量、一条高优先级通信流。它的生命周期协议要回答三个问题：

1. **何时分配**——构造期一次性全量分配，此后零分配（静态形状承诺的一部分）。
2. **如何释放**——`destroy()` 必须在**拆除进程组之前**调用，且内部顺序有讲究：对称内存被远端 rank 直接读写，任何 rank 单方面提前解映射都可能让别的 rank 的在途写落到无效地址上。
3. **忘了调 destroy 怎么办**——默认 `__del__` 兜底自动销毁；`explicitly_destroy=True` 则翻转为「只警告不补救」，给要求确定性销毁的框架用。

#### 4.2.2 核心流程

Buffer 的状态机：

```text
构造 __init__
  ├─ _create_context()      # 分配 hidden_buf / meta_buf / scratch，清零屏障区，dist.barrier
  └─ torch.cuda.Stream(priority)   # 创建唯一通信流
        │
        ▼
状态: _ctx ≠ None, _destroyed = False      ← dispatch/prefetch/combine/reduce_grad 可用
        │
        ▼
destroy()   [幂等]
  1. comm_stream.synchronize()   # 通信流上排队的内核全部完成
  2. torch.cuda.synchronize()    # 本设备所有工作完成
  3. dist.barrier()              # 全组到达同一位置： nobody 还在写我的 chunk
  4. 按「视图先于属主」顺序 pop ctx 中的张量引用
     → Python 引用归零 → 张量删除器解除 VMM 映射、释放组播 handle
  5. _ctx = None, _destroyed = True
        │
        ▼
状态: _destroyed = True          ← 任何 API 调用经 _require_ctx 立即断言失败
```

调优参数的影响面一览（全部在构造期决定，运行期不可变）：

| 参数 | 默认 | 影响什么 | 出处 |
| --- | --- | --- | --- |
| `num_sms` | 32 | planning/dispatch/combine/prefetch/grad_reduce 等持久化内核的 CTA 数（启动几何） | api.py L253-L254 |
| `MOONEP_NUM_SMS_DEDUP`（环境变量） | 满卡 | epilogue/prologue 的 grid 大小；刻意做成环境变量便于基准扫描而不污染公共 API | api.py L82-L102 |
| `token_padding` | 128 | 接收段对齐粒度；NvS = S·K + (tp−1)·2·E/R，直接决定 hidden_buf 显存量与 GEMM 段对齐 | api.py L271-L272, L287-L288 |
| `B` | E/R | 预取槽数：训练必须 B=E/R；推理推荐 3–4；决定权重/梯度缓冲第 0 维 | api.py L273-L275；README L56-L59 |
| `comm_stream_priority` | −1 | 通信流优先级（高于主流） | api.py L459 |
| `enable_pdl` | True | 内核间 PDL 衔接开关 | api.py L460 |
| `explicitly_destroy` | False | `__del__` 行为：自动销毁 vs 仅警告 | api.py L461 |

#### 4.2.3 源码精读

**构造期急切分配**：

- [moonep/api.py:L448-L508](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L448-L508) — `Buffer.__init__` 全量签名。`num_sms`、`token_padding`、`B`、`comm_stream_priority`、`enable_pdl`、`explicitly_destroy` 六个旋钮都在这里；第 498-L504 行调用 `_create_context`，第 505-L508 行创建通信流。
- [moonep/api.py:L253-L256](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L253-L256) — `num_sms=None` 落到默认 32，并断言为正整数。
- [moonep/api.py:L273-L275](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L273-L275) — `B=None` 落到 `epn = E // R`。README 对 B 的约定见 [README.md:L56-L59](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L56-L59)：训练必须 `B = E/R`（规划器保证每 rank 至多复制一个远程 home group 的专家），推理 `B = 3–4` 即可、溢出部分经对称映射远程直读不影响正确性。
- [moonep/api.py:L287-L288](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L287-L288) — `token_padding_extra = (token_padding - 1) * 2 * epn`，`NvS = S*K + token_padding_extra`：调大 `token_padding` 换取更整齐的 GEMM 段，代价线性体现在 `hidden_buf` 行数上。
- [moonep/api.py:L82-L102](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L82-L102) — `_num_sms_dedup_from_env`：读取 `MOONEP_NUM_SMS_DEDUP`，校验 `1 ≤ value ≤ 满卡 SM 数`，否则 `ValueError`；缺失时用满卡。docstring 明说了设计动机——基准作业需要扫描它而不动公共 API。

**destroy 协议**：

- [moonep/api.py:L539-L580](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L539-L580) — `destroy()` 主体，幂等（L544-L546 先查 `_destroyed` 直接返回）。
- [moonep/api.py:L551-L556](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L551-L556) — 同步三连：先 `comm_stream.synchronize()`（通信流可能与主流不同步），再 `torch.cuda.synchronize()`（本设备兜底），再 `dist.barrier()`（全组对齐；`dist.is_initialized()` 检查容忍进程组已不在的退化场景）。**顺序不可换**：若把 barrier 提到同步之前，别的 rank 可能在我还 有在途内核时就解除映射；若不解同步，我自己的通信流内核可能在 dist.barrier 的 NCCL 内核之后仍然在写共享区。
- [moonep/api.py:L558-L577](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L558-L577) — 「Drop views before owners」：注释明说张量删除器在 Python 引用消失时释放 VMM 映射与组播 handle；`hidden_buf_local`/`weights_buf_local` 这两个**视图**必须先于 `meta_buf`/`hidden_buf` **属主**被 pop，否则视图仍持引用、属主的释放被延迟到解释器退出。`meta_mc`（组播对象）也排在 `meta_buf` 之前。
- [moonep/api.py:L534-L537](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L534-L537) — `_require_ctx`：销毁后任何 API 调用立即触发 `"MoonEP Buffer has been destroyed"` 断言，fail-fast 而不是段错误。

**`__del__` 与 `explicitly_destroy`**：

- [moonep/api.py:L582-L597](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L582-L597) — 默认路径在 GC 时静默调用 `destroy()`（异常降级为 `ResourceWarning`）；`explicitly_destroy=True` 时改为只发 `"resources may leak"` 警告。前者是安全默认，后者服务确定性场景：GC 时机不可控，且解释器关闭阶段 `dist`/CUDA 可能已不可用，自动销毁本身可能失败。
- [README.md:L175-L178](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L175-L178) — 官方用法示例的最后一行就是 `buffer.destroy()`，与 Buffer 类 docstring（[moonep/api.py:L444-L446](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L444-L446)）"Call ``destroy()`` before tearing down the process group" 呼应：`destroy()` 内部的 `dist.barrier()` 要求进程组仍然存活，**顺序必须是 destroy → 再拆进程组**。

#### 4.2.4 代码实践：生命周期协议的纯 Python 模拟

**实践目标**：不依赖 GPU，用 mock 复现 destroy 协议的**顺序约束**，验证「视图先于属主释放」与幂等性。

**操作步骤**（示例代码，纯 Python 可运行）：

```python
# lifecycle_mock.py —— 用引用计数模拟 VMM 属主/视图释放顺序
class Owner:                     # 模拟 meta_buf / hidden_buf（属主，删除时解除映射）
    def __init__(self, name): self.name = name
    def __del__(self): print(f"  release VMM mapping: {self.name}")

class View:                      # 模拟 hidden_buf_local 等视图（只是引用，不是属主）
    def __init__(self, name, owner): self.name, self.owner = name, owner

class MockBuffer:
    DESTROY_ORDER = ("hidden_buf_local", "weights_buf_local", "meta_mc",
                     "meta_buf", "hidden_buf", "alloc", "grid_sync_bar")
    def __init__(self):
        self._destroyed = False
        meta = Owner("meta_buf"); hidden = Owner("hidden_buf")
        self._ctx = {"meta_buf": meta, "hidden_buf": hidden,
                     "meta_mc": Owner("meta_mc"),
                     "hidden_buf_local": View("hidden_buf_local", hidden),
                     "weights_buf_local": View("weights_buf_local", meta),
                     "alloc": Owner("alloc"), "grid_sync_bar": Owner("grid_sync_bar")}
    def destroy(self):
        if self._destroyed: print("  (idempotent: already destroyed)"); return
        print("1. comm_stream.synchronize()\n2. torch.cuda.synchronize()\n3. dist.barrier()")
        for k in self.DESTROY_ORDER:            # api.py L560-L575 的顺序
            self._ctx.pop(k, None)
        self._ctx.clear(); self._destroyed = True
        print("4. _ctx = None, _destroyed = True")

b = MockBuffer(); b.destroy(); b.destroy()      # 第二次应打印幂等提示
```

**需要观察的现象**：控制台按 destroy 顺序打印释放日志——`meta_buf` 的 VMM 释放打印发生在两个视图被 pop **之后**；第二次 `destroy()` 不再释放任何资源。

**预期结果**：若把 `DESTROY_ORDER` 中 `meta_buf` 挪到 `weights_buf_local` 之前，你会发现（在真实 `__del__` 语义下）`meta_buf` 的映射解除仍被视图引用推迟——这正是 MoonEP 把视图排在属主前面的原因。mock 里 `View` 持有 `owner` 引用，可通过 `del` 后接 `gc.collect()` 观察差异（待本地验证，效果依赖 CPython 引用计数实现）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `MOONEP_NUM_SMS_DEDUP` 做成环境变量而不是 `Buffer` 的构造参数？

**答案**：见 [moonep/api.py:L83-L88](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L83-L88) 的 docstring：基准作业要在不改调用代码的情况下扫描这个值（同一脚本跑多组 SM 配置），若它是公共 API 参数，每次实验都要改用户代码或加透传链。这是「实验旋钮不进公共接口」的接口卫生原则。

**练习 2**：一个训练框架在 epoch 结束时重建进程组但不重建 Buffer，随后调用 `buffer.reduce_grad(...)` 会发生什么？

**答案**：`reduce_grad` 内部第一个动作就是 `self._require_ctx()`（L1121）。若 Buffer 未 destroy，`_ctx` 仍有效，调用不会立刻报错——但 Buffer 内部的 `group` 句柄指向已被拆除的进程组，`destroy()` 末尾的 `dist.barrier(group=...)` 或任何集合操作将行为未定义。正确做法：拆进程组前显式 `destroy()`，重建后新建 Buffer。（`dist.is_initialized()` 检查只兜底「进程组已整体消失」的场景。）

**练习 3**：把 `token_padding` 从 128 调到 256，`NvS`、显存与 GEMM 行为各怎么变？

**答案**：由 [moonep/api.py:L287-L288](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/api.py#L287-L288)，padding 余量从 `(128−1)·2·E/R` 变为 `(256−1)·2·E/R`，即 `NvS` 增大 `2·E/R`（E/R=32 时多 64 行），`hidden_buf` 与 WEIGHTS 区随之变大（还受 VMM 粒度对齐的二次放大，见 u2-l4）；换来的是每个非空专家段对齐到 256 的倍数，组 GEMM 的每段形状更整齐。浪费上界是每段至多 255 个 padding 行——由零填充 warp 保证读到零（u4-l3）。

### 4.3 构建配置与新设备适配的改动清单

#### 4.3.1 概念说明

MoonEP 的构建是「**一个 AOT + 一个 JIT**」的双轨制：

- **AOT 轨**：`setup.py` 把 `csrc/bindings.cu` 编译成 `moonep._C`（pybind11 扩展），封装 CUDA **驱动 API**（`cuMemCreate`/`cuMemMap` 组、`cuMulticast*` 组、fabric 句柄）。这是纯 Python 做不到的部分，所以需要提前编译并链接 `libcuda`。
- **JIT 轨**：`moonep/` 下的 CuTe DSL 内核在首次调用时由 `nvidia-cutlass-dsl` 现场编译，`_get_compiled` 系列的 `lru_cache` 保证每个（形状, dtype, 设备）组合只编译一次。

这个划分决定了两类二次开发的成本结构：改 Python 内核**不需要重新构建**（JIT 自动生效）；改 `csrc/` 才需要重跑 `pip install -e .`。而「移植到新设备」会同时击穿两条轨——驱动 API 层、PTX 指令层、PyTorch 设备假设层三处都是 CUDA 专属。

#### 4.3.2 核心流程

```text
构建期 (AOT)                                运行期 (JIT)
setup.py                                    moonep/api.py 首次调用
  └─ CUDAExtension("moonep._C",               └─ launch_*(...)
       sources=[csrc/bindings.cu],                 └─ _get_compiled(...)   ← lru_cache
       libraries=["cuda"])                             └─ cute.compile → cubin
  └─ nvcc_flags / cxx_flags                依赖: nvidia-cutlass-dsl==4.4.2 (install_requires)
```

新设备（例如 README 提到的 Zhenwu PPU）的移植面按依赖深度分三层：

| 层 | 内容 | 规模 |
| --- | --- | --- |
| 驱动 API 层 | `csrc/` 全部：VMM 分配/映射、fabric 句柄、NVSwitch 组播，11 个导出符号 | 一个 C++ 文件，但语义全部 CUDA 专属 |
| 指令层 | `moonep/_common.py` 的 PTX inline asm：`cp.async.bulk`、mbarrier、`multimem`、原子/栅栏（u4-l1） | 十余个助手，逐条替换为目标 ISA 等价物 |
| 运行时假设层 | `moonep/` 里 28 处 `torch.cuda.*`（其中 api.py 占 14 处）+ CuTe DSL 本身 | 分散但机械 |

算法层（规划器的均衡算法、去重编码、槽位计算）是数学而非 CUDA，理论上可随 DSL 迁移，但当前 CuTe DSL 本身只面向 NVIDIA GPU。

#### 4.3.3 源码精读

**构建入口与双模式**：

- [setup.py:L1-L13](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/setup.py#L1-L13) — 模块 docstring 写明两种构建方式：`pip install -e .`（可编辑安装，首选）与 `python setup.py build_ext --inplace`（就地构建），产物都是 `moonep/_C.<abi-tag>.so`。
- [setup.py:L50-L65](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/setup.py#L50-L65) — `CUDAExtension(name="moonep._C", sources=["csrc/bindings.cu"], include_dirs=[csrc], libraries=["cuda"], ...)`。`libraries=["cuda"]` 链接**驱动 API 库**（不是运行时 `cudart`），对应 u2-l2 精读过的 `cuMemCreate` 系调用。
- [setup.py:L72-L74](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/setup.py#L72-L74) — `install_requires=["nvidia-cutlass-dsl==4.4.2"]`：版本**精确钉死**。原因在 u4-l1 讲过——`_common.py` 的 inline asm 是为绕开该版本的两个 TMA lowering 限制（box 256 元素上限、mbarrier 状态空间不匹配）而写的，DSL 升级后 workaround 可能失效或多余，升级需要连带回归测试。

**编译开关**：

- [setup.py:L24-L41](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/setup.py#L24-L41) — `nvcc_flags`：`-O3 --use_fast_math -std=c++20` 等常规项之外，末尾四个宏值得注意。
- [setup.py:L37-L40](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/setup.py#L37-L40) — `__CUDA_NO_HALF_OPERATORS__ / __CUDA_NO_HALF_CONVERSIONS__ / __CUDA_NO_BFLOAT16_CONVERSIONS__ / __CUDA_NO_HALF2_OPERATORS__`：禁用 CUDA C++ 头文件里 half/bf16 的隐式转换与向量运算。**与本讲 4.1 的 fp16 实践无关**（那只动 Python 层），但它们意味着：如果你未来在 `csrc/` 里手写涉及 `__half` 的 C++ 代码，所有转换必须显式，混用 dtype 会在编译期报错——把精度事故前移到构建期。
- [setup.py:L43-L47](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/setup.py#L43-L47) — host 侧 `cxx_flags` 只有 `-O3 -fno-strict-aliasing -Wno-psabi`：`-fno-strict-aliasing` 是 pybind11 覆盖 Python 对象时的常规防御。

**C++ 移植面的完整清单**：

- [csrc/bindings.cu:L13-L50](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L13-L50) — `PYBIND11_MODULE(_C, m)` 导出 1 个常量 `FABRIC_HANDLE_BYTES` 与 11 个函数：`nvl_dist_alloc`、`nvl_dist_map`、`nvl_fabric_supported`、`get_vmm_granularity`、`get_multicast_granularity`、`nvl_multicast_supported`、`nvl_multicast_create`、`nvl_multicast_import`、`nvl_multicast_add_device`、`nvl_multicast_bind_map`、`nvl_release_mem_handle`。**这份清单就是新设备后端必须逐个提供的接口**——`moonep/buffer.py` 是它们的唯一消费者（u1-l3 已用 import 邻接表验证）。
- [moonep/prefetch.py:L294-L296](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/prefetch.py#L294-L296) — 设备能力查询的例子：`shared_memory_per_block_optin` 来自 `torch.cuda.get_device_properties`——这类「运行时向设备问参数」的点是设备假设层最密集的地方。

**设备支持现状**：

- [README.md:L34-L37](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L34-L37) — Supported Devices 列出 NVIDIA GPU 与「Zhenwu PPU (under review, coming soon)」。**当前仓库中不存在任何 PPU 相关代码**（全仓库检索 `zhenwu|ppu` 只命中 README 一处），所以 PPU 适配的状态是待确认——不应在讲义里描述其实现细节。

#### 4.3.4 代码实践：CUDA 依赖面盘点（只读仓库即可完成）

**实践目标**：亲手量出新设备移植的真实表面积，产出一份可执行的改动清单。

**操作步骤**：

1. 在仓库根目录执行下面的检索（示例命令），统计 `torch.cuda` 的分布：

```bash
grep -rn "torch\.cuda" moonep/ | awk -F: '{print $1}' | sort | uniq -c
grep -c "inline_asm" moonep/_common.py
```

2. 打开 [csrc/bindings.cu](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/csrc/bindings.cu#L13-L50)，把 11 个导出符号按「分配/映射（`nvl_dist_*`）、探测（`*_supported`、`*_granularity`）、组播（`nvl_multicast_*`）、释放（`nvl_release_*`）」四类抄成表格。
3. 对照 [moonep/buffer.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/moonep/buffer.py) 找出每个符号的 Python 包装函数，标注哪些被 `_create_context` 依赖（提示：`get_vmm_granularity`、`get_multicast_granularity` 决定 chunk 双重对齐，u2-l4）。

**需要观察的现象**：`torch.cuda` 在 `moonep/` 9 个文件中共 28 处（api.py 14 处最密——集中在 `_create_context` 的设备属性查询与销毁同步）；`_common.py` 中 inline asm 助手十余个。

**预期结果**：你会得到一张三层清单：C++ 层 1 个文件 11 个符号、指令层 1 个文件十余个助手、运行时假设层 28 处分散调用。结论：**MoonEP 没有设备后端抽象**，移植是重写而非插拔；但 `buffer.py` 作为 `_C` 的唯一消费者，天然是未来若要引入后端接口时的切分线。

#### 4.3.5 小练习与答案

**练习 1**：改了 `moonep/dispatch.py` 里的一行内核代码，需要重新 `pip install -e .` 吗？

**答案**：不需要。`moonep/` 是 CuTe DSL JIT 路径，`cute.compile` 在首次调用时编译；可编辑安装下 Python 源码即改即生效（第一次调用会重新走 `_get_compiled` 的 `lru_cache`——注意若只改了设备代码而 `lru_cache` 键未变，同进程内已缓存的 cubin 不会重编，重启进程即可）。只有 `csrc/` 的改动才需要重跑构建。

**练习 2**：`nvidia-cutlass-dsl==4.4.2` 为什么用 `==` 精确钉死而不是 `>=`？

**答案**：`_common.py` 的 PTX inline asm 是针对该版本的 TMA lowering 缺陷写的绕行方案（u4-l1）。次版本更新可能改变 lowering 行为，使绕行失效（或引入新的不兼容）；在未跑完六组多卡测试之前，宽松上界等于把风险交给用户。升级 DSL 是一个需要显式回归的变更，不是日常兼容项。

**练习 3**：如果要在 `csrc/` 中新增一个导出函数 `nvl_get_device_name()`，构建配置里哪一项保证了 `cuDeviceGetName` 这类驱动 API 能被链接到？

**答案**：[setup.py:L59](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/setup.py#L59) 的 `libraries=["cuda"]`——它让扩展链接 CUDA 驱动库，所有 `cu*` 前缀的驱动 API 符号由它解析；`include_dirs`（L56-L58）则保证 `csrc/` 内部头文件（如 `nvl_shared_buffer.cuh`）可被找到。

## 5. 综合实践

**任务：给 `prefetch_weight` 增加 float16 权重支持（最小改动 + 断言/测试清单 + 验证步骤）。**

这是贯穿 4.1/4.2/4.3 的完整二次开发演练：改类型注册表（4.1），不动 Buffer 与构建（4.2/4.3 的「哪些不用改」判断本身就是考点）。

### 5.1 改动方案

核心改动只有 `moonep/prefetch.py` 两处（以下为修改草案，示例代码）：

```python
# 1) moonep/prefetch.py L28 —— 导入 CuTe 的 fp16 数值类型
-from cutlass import BFloat16, Int8, Int64, Uint8
+from cutlass import BFloat16, Float16, Int8, Int64, Uint8

# 2) moonep/prefetch.py L32-L36 —— 注册表加一行
 _ELEM_TYPES = {
     torch.bfloat16: (BFloat16, 2),
+    torch.float16: (Float16, 2),
     torch.int8: (Int8, 1),
     torch.uint8: (Uint8, 1),
 }
```

**`_smem_bytes` 的 elem_bytes 变化分析**（呼应 4.1.4 的 dry-run）：float16 与 bfloat16 同为 2 字节，因此 `tile_bytes = 128·128·2` 不变、`_pick_stages` 选档不变、`tx_count = TILE_BYTES` 不变、`stage_smem` 的字节占用不变。**数值上零变化**；注册表把 `(Float16, 2)` 经由 `PrefetchKernel.__init__` → `const_expr` → `make_ptr` 传完整条参数化路径，唯一的「新东西」是编译期指针/smem 类型标签，以及 `_get_compiled` 因 `torch_dtype` 进键而多出的一份独立 cubin。

### 5.2 需要同步修改的断言与测试清单

| 位置 | 是否要改 | 原因 |
| --- | --- | --- |
| `launch_prefetch` 的 `assert dtype in _ELEM_TYPES`（prefetch.py L379-L382） | **不用改** | 遍历的是同一张注册表，加条目后自动放行 |
| `Buffer.prefetch_weight` 的 `assert w.dtype in _ELEM_TYPES`（api.py L903-L905） | **不用改** | api.py L71 从 prefetch 导入的就是 `_ELEM_TYPES` 本体 |
| `prefetch_buffers.dtype == dtype`（prefetch.py L385-L387） | 不用改 | 用户需自行保证源/目标同为 fp16 |
| `H/H' % 128` 断言（prefetch.py L401-L402） | 不用改 | 形状约束与 dtype 无关 |
| `combine` 的 bf16 断言（api.py L1001-L1004） | **不能改** | hidden 通路与权重通路无关，混入会破坏契约 |
| 梯度 fp32 断言（api.py L204-L208） | **不能改** | 预取槽梯度归约的精度前提（u5-l3） |
| `tests/test_prefetch.py` CASES | **建议加一个 case** | 见下 |
| `benchmarks/bench_prefetch.py` `_DT_LABEL`（L37-L42） | 可选 | 仅当想让基准脚本接受 `--dtype fp16` 时加 `torch.float16: "fp16"` |

新增测试用例（插入 [tests/test_prefetch.py](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L46-L203) 的 `CASES`，照抄 uint8 量化 case 的模式 [L126-L137](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L126-L137)）：

```python
{
    "name": "fp16_rect_duplicates",
    "E": 8, "H": 256, "Hp": 384, "B": 4, "num_sms": 16,
    "owner_offset": 1,
    "dtype": torch.float16,
    "experts": lambda E: [E - 1, 0, E // 2, -1],
},
```

测试框架无需任何改动：`_fill_random` 对浮点 dtype 统一走 `torch.randn`（[tests/test_prefetch.py:L229-L235](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L229-L235)），`_sentinel_for` 对浮点返回 `-123.0`（[L238-L241](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L238-L241)），`parametrize` 按 `case["name"]` 自动展开（[L335-L340](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/tests/test_prefetch.py#L335-L340)）。断言口径是**逐位相等**（`torch.equal`），fp16 整块拷贝不经任何算术，理应逐位一致。

### 5.3 验证步骤

1. **前置检查**：`python -c "from cutlass import Float16; print('ok')"`——确认 `nvidia-cutlass-dsl==4.4.2` 顶层导出 `Float16`（与 `BFloat16` 同源；本讲义撰写环境未安装该包，**待本地验证**）。
2. **单元测试**：`torchrun --nproc_per_node=8 -m pytest tests/test_prefetch.py -k fp16`——多卡 + NVLink 环境下跑新增 case；观察 `[PASS] fp16_rect_duplicates ... dtype=torch.float16` 输出。
3. **回归**：跑全部六组测试（README [L180-L192](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L180-L192)），确认既有 dtype 路径未受影响（`_get_compiled` 按 dtype 分键缓存，理论上隔离）。
4. **集成冒烟**：在 u6-l3 的训练步脚本里把三个 `full_*_weight` 换成 fp16 张量调用 `buffer.prefetch_weight`，确认不再触发 `unsupported weight dtype` 断言，且预取槽内容与源专家行逐位一致（可复用 `assert_prefetched` 式检查：`torch.equal(w[E + b], w[e])`）。
5. **可选基准**：`torchrun --nproc_per_node=8 benchmarks/bench_prefetch.py` 对比 fp16 与 bf16 的预取带宽——二者字节数相同，带宽应基本持平（**待本地验证**）。

以上步骤 2-5 需要多 GPU + NVLink 环境；本讲义环境无法代跑，结果以你本地输出为准。

## 6. 本讲小结

- **权重通路是 MoonEP 设计好的 dtype 扩展点**：`_ELEM_TYPES` 一张表把 `torch.dtype` 映射为（CuTe 类型, 字节数），沿 `launch_prefetch → _get_compiled → PrefetchKernel → smem/tx_count/指针` 全程参数化；hidden（bf16）与梯度（fp32）通路则是硬编码，扩展成本高一个量级。
- **float16 的最小改动只有两行**（导入 `Float16` + 注册表加条目），因为 fp16 与 bf16 同为 2 字节，`_smem_bytes`、级数选择与事务计数数值全不变；`launch_prefetch` 与 `prefetch_weight` 的白名单断言共享注册表、自动放行。
- **Buffer 生命周期协议**：构造期急切分配全部对称内存；`destroy()` 幂等，顺序为「同步通信流 → 同步设备 → 集合屏障 → 按视图先于属主释放引用」，且必须发生在拆除进程组之前；`explicitly_destroy=True` 把 GC 兜底换成警告。
- **调优旋钮的影响面**：`B` 决定权重/梯度缓冲与预取容量（训练必须 E/R）；`token_padding` 线性放大 `NvS` 换 GEMM 段整齐；`num_sms`/`MOONEP_NUM_SMS_DEDUP` 分别控制主内核与 epilogue/prologue 的启动几何。
- **构建是 AOT（`moonep._C`，链接驱动 API）+ JIT（CuTe DSL，`lru_cache` 缓存 cubin）双轨**：改 Python 内核不重建、改 `csrc` 才重建；`nvidia-cutlass-dsl==4.4.2` 精确钉死是因为 PTX workaround 绑定该版本。
- **新设备移植没有现成后端抽象**：移植面 = `csrc/` 的 11 个导出符号 + `_common.py` 的 PTX 助手 + 28 处 `torch.cuda` 假设；Zhenwu PPU 支持目前只是 README 的一行「under review」（待确认，仓库无对应代码）。

## 7. 下一步学习建议

本讲是手册最后一讲。至此你已从 README 走到 PTX 指令、从均衡算法走到生命周期协议，接下来建议：

1. **动手做 5.1 的 float16 补丁**并按 5.3 验证——这是检验你是否真正掌握类型注册表的最短路径；做完可以再尝试更有挑战的 `torch.float32`（体会 `elem_bytes=4` 引起的掉档）。
2. **读一遍 `benchmarks/bench_comm.py` 的参数扫描结构**，然后用 `MOONEP_NUM_SMS_DEDUP` 环境变量实扫一组 epilogue SM 数，把 4.2 的「影响面」表格变成实测曲线。
3. **把 MoonEP 接进一个最小 Megatron 风格训练循环**：以 u6-l3 的四象限为骨架、以 README「Integration」一节（[README.md:L41-L69](https://github.com/MoonshotAI/MoonEP/blob/2bd860b4dd083df62b79d5e916fca71ec5742228/README.md#L41-L69)）的 `cu_seqlens` + 单根 `[E+B,H,H']` 权重契约为接口，替换你现有 MoE 层的 all-to-all。
4. **横向对比阅读**：README 致谢中列出的 DeepEP、Echo、UltraEP 等库，重点比较它们与 MoonEP 在「均衡策略（冗余专家 vs 容量放宽）」「形状策略（静态 vs 动态）」上的取舍，验证你在 u1-l1 学到的 maxvio 问题框架。
5. 若你在非 NVIDIA 设备上工作，可以把 4.3.4 的盘点清单作为立项文档的第一节——但在 PPU 支持合入上游之前（README 标注 under review），请以仓库实际代码为准。

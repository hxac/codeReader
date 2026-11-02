# 显存优化与 OOM 重试机制

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行讲清 `retry_if_cuda_oom` 的**三段降级策略**：直接执行 → 清空 CUDA 缓存重试 → 把输入搬到 CPU（转 float32、关闭 autocast）再试。
2. 解释 `_ignore_torch_cuda_oom` 这个上下文管理器如何**只吞掉 CUDA OOM 异常**、其他异常照常抛出，以及这种"异常即控制流"写法的原理与风险。
3. 准确统计 Mask2Former 推理路径上所有 `retry_if_cuda_oom` 的包裹点（图像版 5 处调用行、视频版 2 处调用行），并说明图像版与视频版 import 的是**两份不同来源的同名实现**。
4. 理解高分辨率推理各阶段的内存瓶颈在哪，以及 `sem_seg_postprocess_before_inference` 这个开关如何**从流程设计上规避 OOM**（而不是靠重试）。

本讲是单元十的工程化主题，承接 u6-l1（语义推理分支中首次见过 `retry_if_cuda_oom` 的调用）与 u6-l3（全景推理分支），把当时的"黑盒兜底机制"拆开看实现。

## 2. 前置知识

### 2.1 CUDA 显存与 PyTorch 的缓存分配器

PyTorch 在 GPU 上分配张量时，并不直接找 CUDA 驱动要内存，而是通过一个**缓存分配器（caching allocator）**工作：

- 张量被释放后，显存块并不立刻还给驱动，而是留在分配器的"储备池"里，下次同样大小的分配直接复用——这是为了速度。
- 后果：`nvidia-smi` 看到的进程显存占用只增不减；一个"总量够、但没有连续大块"的碎片化状态，同样会触发 OOM。
- `torch.cuda.empty_cache()` 会把储备池中**空闲**的块归还给驱动，相当于一次"碎片整理"。这正是"清缓存后重试可能成功"的原理：重试时分配器有机会拿到一个更大的连续块。

### 2.2 CUDA OOM 长什么样

PyTorch 抛出的 CUDA OOM 是一个 `RuntimeError`，消息形如：

```
CUDA out of memory. Tried to allocate 2.00 GiB (GPU 0; 23.65 GiB total capacity; ...)
```

注意两点：它是一个普通的 `RuntimeError`（不是独立异常类型）；识别它的唯一途径是**匹配消息字符串**。这两点是本讲主角 `_ignore_torch_cuda_oom` 全部设计取舍的来源。

### 2.3 装饰器与"异常即控制流"

- 装饰器 `retry_if_cuda_oom(func)` 返回一个包装函数，调用方式是 `retry_if_cuda_oom(some_func)(args)`——先包再调。
- `@contextmanager` 把一个带 `yield` 的函数变成 `with` 语句可用的上下文管理器：`yield` 前的代码相当于 `__enter__`，`yield` 后的相当于 `__exit__`。
- 本讲代码里最精妙的一招是：**`return` 写在 `with` 块内部**。函数成功执行时 `return` 直接带着返回值离开；函数抛出 CUDA OOM 且被上下文管理器吞掉时，`with` 块正常退出，控制流"落"到下一行继续执行重试逻辑。异常本身变成了分支语句。

### 2.4 半精度与 autocast

- 混合精度训练/推理时，张量可能是 `float16`。CPU 对 fp16 算子的支持远不如 GPU 完整，因此把张量搬到 CPU 时通常顺手转成 `float32`。
- `autocast` 是自动混合精度的开关上下文。降级到 CPU 重试时用 `autocast(enabled=False)` 显式关闭，防止外层的 AMP 设置"漏"进来。

### 2.5 三种尺寸的回顾（承接 u3-l1 / u6-l1）

- **原图尺寸** (H, W)：数据集里图片的真实大小，高分辨率数据集（如 Mapillary）可达数千×数千。
- **网络输入尺寸**：测试时按 `INPUT.MIN_SIZE_TEST` 缩放后的尺寸。
- **padding 尺寸**：网络输入再按 `size_divisibility`（如 32）补齐后的尺寸，即 `images.tensor` 的形状。
- 模型的 `pred_masks` 是 1/4 分辨率，推理时先上采样到 padding 尺寸，之后每一步的显存开销取决于后续算子**在哪一种尺寸上工作**——这是第 4.4 节的核心。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [mask2former_video/utils/memory.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py) | 仓库内自带的 `retry_if_cuda_oom` / `_ignore_torch_cuda_oom` 实现（全文件仅 81 行，只依赖 torch，**视频包**消费它） |
| [mask2former/maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py) | 图像版 `MaskFormer`，从 **detectron2** 导入同名工具，在推理 eval 分支有 5 处调用行 |
| [mask2former_video/video_maskformer_model.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py) | 视频版 `VideoMaskFormer`，从**仓库内副本**导入，2 处调用行 |
| [mask2former/config.py](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py) | `SEM_SEG_POSTPROCESSING_BEFORE_INFERENCE` 的默认值注册处 |

一个容易混淆的事实先摆出来：**仓库里有两份同源的 `retry_if_cuda_oom`**。图像版模型用的是 detectron2 里的那份（[maskformer_model.py:L14](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L14)：`from detectron2.utils.memory import retry_if_cuda_oom`）；视频包自带一份副本（[video_maskformer_model.py:L19](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L19)：`from .utils.memory import retry_if_cuda_oom`）。两者都是"直接执行 → 清缓存重试 → 转 CPU"的三段降级，行为一致（u6-l1 已从调用方视角确认）。本讲精读仓库内这份副本的实现，结论对两份都成立。

## 4. 核心概念与源码讲解

### 4.1 模块一：`_ignore_torch_cuda_oom`——只吞 CUDA OOM 的异常过滤器

#### 4.1.1 概念说明

重试机制的第一块积木是一个上下文管理器：在它的 `with` 块里发生的异常，**只有 CUDA OOM 会被静默吞掉**，其他异常原样抛出。

为什么需要"过滤"而不是"全吞"？因为 `RuntimeError` 是个筐，什么错误都往里装——维度不匹配、算子不存在、cuDNN 报错都是 `RuntimeError`。如果把所有 `RuntimeError` 都吞掉再重试，一个真正的 bug（比如形状错误）就会被悄悄重试三次、再被搬到 CPU 上继续失败，把一个清晰的报错变成一场混乱。所以过滤条件必须尽量窄：只匹配 CUDA OOM 的消息字符串。

#### 4.1.2 核心流程

```
with _ignore_torch_cuda_oom():
    执行代码
    │
    ├─ 正常结束 → 退出 with，继续后续语句
    ├─ 抛出 RuntimeError 且消息含 "CUDA out of memory. " → 吞掉，正常退出 with
    └─ 抛出其他异常（或消息不匹配的 RuntimeError）→ 重新抛出（re-raise）
```

#### 4.1.3 源码精读

[memory.py:L12-L24](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py#L12-L24)：

```python
@contextmanager
def _ignore_torch_cuda_oom():
    """
    A context which ignores CUDA OOM exception from pytorch.
    """
    try:
        yield
    except RuntimeError as e:
        # NOTE: the string may change?
        if "CUDA out of memory. " in str(e):
            pass
        else:
            raise
```

逐行拆解：

- L17-18：`try: yield`——被包裹的代码体在此执行，异常沿 `yield` 抛回本函数。
- L19：只捕获 `RuntimeError`；其他类型（如 `IndexError`）直接穿透。
- L21：匹配字符串是 `"CUDA out of memory. "`，**带一个尾随空格**——PyTorch 的消息是 `CUDA out of memory. Tried to allocate ...`，句点后的空格能把这条消息和其它只是"提到" out of memory 的消息区分开，进一步收窄匹配。
- L20：作者自己留的注释 `# NOTE: the string may change?`——坦承这是**脆弱的字符串匹配**：PyTorch 某天改了消息格式，过滤器就会失配。失配的后果是 OOM 不再被吞、直接向上抛出，重试机制整体退化为"不重试"——这是一个**保守方向的失效**（宁可不做兜底，也不误吞别的异常），设计上是自洽的。

#### 4.1.4 代码实践

**实践目标**：验证异常过滤器"只吞 CUDA OOM、其他照抛"的行为。这个实验在**纯 CPU 环境**就能跑——因为过滤器只看消息字符串，根本不关心是否真有 GPU。

操作步骤（示例代码）：

```python
# 示例代码：CPU 环境即可验证 _ignore_torch_cuda_oom 的过滤行为
from mask2former_video.utils.memory import _ignore_torch_cuda_oom

# 场景 1：消息匹配 → 被吞掉
try:
    with _ignore_torch_cuda_oom():
        raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")
    print("场景 1：异常被吞掉，执行到这一行")
except RuntimeError:
    print("场景 1：异常抛出来了（不符合预期）")

# 场景 2：同为 RuntimeError 但消息不匹配 → 照常抛出
try:
    with _ignore_torch_cuda_oom():
        raise RuntimeError("shape mismatch: (2, 3) vs (4, 5)")
except RuntimeError as e:
    print("场景 2：照常抛出 ->", e)
```

需要观察的现象与预期结果：

- 场景 1 打印"异常被吞掉"；场景 2 的异常穿透 `with` 被 外层 `except` 接住。
- 注意 `_ignore_torch_cuda_oom` 不在模块的 `__all__` 里（[memory.py:L9](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py#L9) 只导出 `retry_if_cuda_oom`），但 Python 的 `from ... import` 不受 `__all__` 限制，直接按名字导入即可。

本环境未验证运行结果（待本地验证）；该文件只依赖 `logging` / `contextlib` / `functools` / `torch`，无需安装 detectron2。

#### 4.1.5 小练习与答案

**练习 1**：如果把 L21 的匹配串改成 `"out of memory"`（去掉前缀和尾随空格），行为会有什么变化？

**答案**：匹配变宽。任何消息里含有 "out of memory" 的 `RuntimeError`（例如用户自定义的报错、CPU 内存相关消息）都会被误吞，从而触发不必要的重试与 CPU 降级，掩盖真实错误。原实现的尾随空格正是为了把匹配钉死在 PyTorch 的标准消息格式上。

**练习 2**：`_ignore_torch_cuda_oom` 吞掉异常后，`with` 块**之后**的语句会不会执行？

**答案**：会。`__exit__` 返回真值（或异常被捕获后不再抛出）意味着 `with` 语句正常结束，控制流继续走 `with` 之后的下一条语句——这正是 4.2 节三段降级得以串联的根基。

---

### 4.2 模块二：`retry_if_cuda_oom`——三段降级装饰器

#### 4.2.1 概念说明

`retry_if_cuda_oom` 把上一节的异常过滤器组装成一个完整的降级策略。它解决的问题：分割模型在**高分辨率图片**上推理时，后处理阶段（上采样、融合、逐像素 argmax）的临时张量可能超过 GPU 显存，但整段推理又不能因此失败——用户希望"慢一点也要出结果"。

它的策略是逐级牺牲速度换成功率：

| 段 | 动作 | 代价 | 成功概率 |
| --- | --- | --- | --- |
| 第 1 段 | 原样执行 | 无 | 正常情况 |
| 第 2 段 | `torch.cuda.empty_cache()` 后重试 | 一次清缓存（较便宜） | 碎片化导致的 OOM |
| 第 3 段 | 输入逐个搬 CPU + 转 float32 + 关 autocast 后重试 | 极慢（PCIe 传输 + CPU 算力） | 只要内存条够就成功 |

对被包裹函数有两条硬性要求（docstring 的 Note，[memory.py:L44-L49](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py#L44-L49)）：

1. **无状态（stateless）**：函数可能被调用最多 3 次，若有副作用（计数、原地修改）会累积。
2. **参数是顶层的张量（或任意对象）**：转 CPU 时只检查每个参数自身的 `.device` 和 `.to`，不递归进入嵌套结构（list/dict 里的张量不会被搬）。

Mask2Former 包裹的函数——`sem_seg_postprocess`、`F.interpolate`、三个 `*_inference` 方法——都是纯函数式的顶层张量运算，恰好满足。

#### 4.2.2 核心流程

```
wrapped(*args, **kwargs):
    第 1 段：with 吞 OOM: return func(args)        # 成功 → 直接返回
        ↓ OOM 被吞，控制流落下
    第 2 段：torch.cuda.empty_cache()
             with 吞 OOM: return func(args)        # 成功 → 直接返回
        ↓ 又 OOM，控制流落下
    第 3 段：log "Attempting to copy inputs to CPU due to CUDA OOM"
             args/kwargs 逐个 maybe_to_cpu（CUDA 张量 → CPU + fp32）
             with autocast(enabled=False):
                 return func(new_args)             # 不再吞异常，失败即上抛
```

用数学描述第 3 段的转换：设输入张量集合为 \(\{x_i\}\)，则

\[
x_i \mapsto \begin{cases} x_i.\text{to}(\text{cpu}).\text{to}(\text{float32}) & \text{if } x_i.\text{device.type} == \text{"cuda"} \\ x_i & \text{otherwise} \end{cases}
\]

#### 4.2.3 源码精读

**第一部分：`maybe_to_cpu` 辅助函数**，[memory.py:L52-L60](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py#L52-L60)：

```python
def maybe_to_cpu(x):
    try:
        like_gpu_tensor = x.device.type == "cuda" and hasattr(x, "to")
    except AttributeError:
        like_gpu_tensor = False
    if like_gpu_tensor:
        return x.to(device="cpu").to(torch.float32)
    else:
        return x
```

这段代码做的是**鸭子类型探测**：

- L54：`x.device.type == "cuda"`——int、float、str、None 都没有 `.device` 属性，触发 `AttributeError`（L55 捕获后判 False），原样返回。所以像 `sem_seg_postprocess(mask, image_size, height, width)` 里的尺寸 int 参数完全不受影响。
- L54 的 `hasattr(x, "to")`：有 `.device` 又有 `.to` 的对象按"类张量"处理，不强求是 `torch.Tensor`。
- L58：`.to(device="cpu").to(torch.float32)` 链式两跳——先搬设备再转精度。转 fp32 的原因：混合精度推理时输入可能是 fp16，而 CPU 算子对 fp16 的支持残缺（此为实现层面的合理推断，docstring 未明说）。

**第二部分：三段降级主体**，[memory.py:L62-L78](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py#L62-L78)：

```python
@wraps(func)
def wrapped(*args, **kwargs):
    with _ignore_torch_cuda_oom():
        return func(*args, **kwargs)          # 第 1 段

    # Clear cache and retry
    torch.cuda.empty_cache()                  # 第 2 段
    with _ignore_torch_cuda_oom():
        return func(*args, **kwargs)

    # Try on CPU. This slows down the code significantly, therefore print a notice.
    logger = logging.getLogger(__name__)
    logger.info("Attempting to copy inputs to CPU due to CUDA OOM")
    new_args = (maybe_to_cpu(x) for x in args)
    new_kwargs = {k: maybe_to_cpu(v) for k, v in kwargs.items()}
    with autocast(enabled=False):
        return func(*new_args, **new_kwargs)  # 第 3 段
```

四个关键点：

1. **L62 `@wraps(func)`**：把原函数的名字、docstring 等元信息拷贝给 `wrapped`，日志与调试器里显示的仍是原函数名。
2. **L64-65 / L69-70：`return` 在 `with` 块里**——这是"异常即控制流"的精髓。成功时 `return` 带值直接离开 `wrapped`；CUDA OOM 被过滤器吞掉时 `with` 正常退出，执行到下一行的 `torch.cuda.empty_cache()`。三段降级不需要一个 `try` 嵌套另一个 `try`，读起来像顺序执行的三个步骤。
3. **L77 `autocast(enabled=False)`**：第 3 段已经搬到 CPU，显式关闭自动混合精度，避免外层若有 AMP 上下文在 CPU 上继续产生半精度中间量（半精度正是转 fp32 想规避的东西）。
4. **第 3 段没有再包 `_ignore_torch_cuda_oom`**：CPU 上不存在 CUDA OOM；若 CPU 也失败（内存不足、算子不支持），异常向上传播是正确行为——这一段已经是最后手段，再吞只会把错误无限推迟。

还有一个值得注意的契约：docstring 明确警告（[memory.py:L33-L35](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py#L33-L35)）**返回值可能变成 CPU 张量**（"The return values may become CPU tensors as well and it's user's responsibility to convert it back"）。也就是说降级成功后，`processed_results` 里的 `sem_seg` / `panoptic_seg` 可能落在 CPU 上——调用方（evaluator、可视化代码）必须容忍设备变化。detectron2 的评估器在消费预测前通常会自行搬移设备，因此这条路径在工程上是通的。

#### 4.2.4 代码实践

**实践目标**：在**纯 CPU 环境**下完整观察三段降级的执行顺序与调用次数。诀窍是：过滤器只认消息字符串，所以我们可以**伪造**消息匹配的 `RuntimeError` 来模拟"前两次 GPU 上 OOM、第三次（CPU 上）成功"。

操作步骤（示例代码）：

```python
# 示例代码：模拟两次 OOM 后第三次成功，观察三段降级顺序
import logging
import torch
from mask2former_video.utils.memory import retry_if_cuda_oom

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s | %(message)s")

calls = []                                   # 记录每次调用（要求被包裹函数无状态，这里只读不动全局状态）

def flaky_upsample(x, size):
    calls.append(len(calls) + 1)
    if len(calls) <= 2:                      # 前两次：模拟 CUDA OOM
        raise RuntimeError(
            "CUDA out of memory. Tried to allocate 4.00 GiB (GPU 0; "
            "23.65 GiB total capacity; 20.00 GiB already allocated)"
        )
    return torch.zeros((x.shape[0], *size))  # 第三次：模拟 CPU 上成功

safe_upsample = retry_if_cuda_oom(flaky_upsample)
out = safe_upsample(torch.zeros(100, 8, 8), (1024, 1024))
print("总调用次数:", len(calls), "| 输出形状:", tuple(out.shape), "| 输出设备:", out.device)
```

需要观察的现象与预期结果：

1. 终端先出现一条 INFO 日志 `Attempting to copy inputs to CPU due to CUDA OOM`（来自 [memory.py:L74](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/utils/memory.py#L74)），然后打印结果——日志出现在成功返回**之前**，证明第三次调用走的是 CPU 降级段。
2. `总调用次数: 3`——第 1 段一次、第 2 段一次、第 3 段一次。注意本环境无 GPU 时 `torch.cuda.empty_cache()`（L68）是安全的无操作（CUDA 未初始化时直接返回），不会报错。
3. 对照实验 A：把 `if len(calls) <= 2` 改成 `<= 1`，则第 2 段（清缓存后）就成功，**不会出现** CPU 日志，总调用次数为 2——验证第 2 段与第 3 段的先后。
4. 对照实验 B：把抛出的消息改成 `"shape mismatch"`，异常会在第 1 段就穿透抛出，总调用次数为 1——验证过滤器只认 CUDA OOM。

以上结果为源码推演的预期（本环境未实际运行，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：第 3 段为什么不再用 `_ignore_torch_cuda_oom` 包裹？

**答案**：CPU 上不存在"CUDA out of memory"这种可通过重试挽救的错误；若 CPU 仍失败（主机内存不足、算子不支持 CPU），异常应当向上传播让调用者看到真实原因。包裹反而会把最后一次失败也变成静默，使函数无法结束。

**练习 2**：`maybe_to_cpu` 能正确处理 `sem_seg_postprocess(mask, image_size, height, width)` 这种"张量 + 多个 int"的混合参数吗？

**答案**：能。int 没有 `.device` 属性，`x.device.type` 抛 `AttributeError` 被 L55 捕获，判定非 GPU 张量、原样返回；只有第一个 mask 张量会被搬运。但若是 `func([mask1, mask2])` 这种**列表**参数，列表本身没有 `.device`，内部张量不会被搬——这就是 docstring Note 1 所说的"嵌套结构不支持"。

**练习 3**：为什么被包裹的函数必须无状态？

**答案**：降级路径下 `func` 最多被执行 3 次。如果函数内部有原地修改输入、累加计数器等副作用，前两次失败的调用留下的副作用会污染第三次的结果，或使输出依赖"恰好第几次成功"——行为不可复现。

---

### 4.3 模块三：图像模型推理路径上的包裹点统计

#### 4.3.1 概念说明

有了装饰器，下一个问题是：**包在哪里？** 原则很简单——包在"显存开销随图片分辨率线性增长、且只有推理时才执行"的后处理算子上，而不是包住整个模型前向。原因有二：

- 训练时的显存由 batch、分辨率裁剪（LSJ 固定 1024×1024）控制，是可控的；推理时要面对任意大的用户图片，**分辨率不可控**才是 OOM 的主因。
- 模型主干（backbone、pixel decoder、Transformer decoder）工作在 1/4~1/32 分辨率上，临时量小；真正的显存大户是 eval 分支里把 `pred_masks` 还原到全尺寸之后的那几步（u6-l1 讲过的上采样与三分支后处理）。

#### 4.3.2 核心流程

`MaskFormer.forward` 的 eval 分支（承接 u6-l1/u6-l3 的流程图，这里只标出防御工事的位置）：

```
outputs ──┬─ pred_logits  = mask_cls_results
          └─ pred_masks   = mask_pred_results (B, Q, H/4, W/4)
                     │
                     ▼
F.interpolate → padding 尺寸                     ← 【无 retry 包装！见 4.3.3 的对比】
                     │  del outputs
                     ▼  逐图循环：
  ┌─ 若 sem_seg_postprocess_before_inference:
  │    retry_if_cuda_oom(sem_seg_postprocess)(mask_pred_result, ...)   ← 包裹点 ①
  ├─ semantic_on:  retry_if_cuda_oom(self.semantic_inference)(...)      ← 包裹点 ②
  │                （after 模式再补）retry_if_cuda_oom(sem_seg_postprocess)(r, ...) ← 包裹点 ③
  ├─ panoptic_on:  retry_if_cuda_oom(self.panoptic_inference)(...)      ← 包裹点 ④
  └─ instance_on:  retry_if_cuda_oom(self.instance_inference)(...)      ← 包裹点 ⑤
```

#### 4.3.3 源码精读

**包裹点 ①：前置尺寸还原**，[maskformer_model.py:L239-L243](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L239-L243)：

```python
if self.sem_seg_postprocess_before_inference:
    mask_pred_result = retry_if_cuda_oom(sem_seg_postprocess)(
        mask_pred_result, image_size, height, width
    )
    mask_cls_result = mask_cls_result.to(mask_pred_result)
```

`sem_seg_postprocess` 把 (Q, h_pad, w_pad) 的 mask 还原到原图 (height, width)，是**全部 Q 个 query 一次性插值**，临时量正比于 Q×H×W——高分辨率下第一个爆显存的候选。注意 L243 的 `.to(mask_pred_result)`：若降级发生、mask 已在 CPU，分类 logits 也跟着搬过去，保证后续 einsum 两操作数同设备。

**包裹点 ②③：语义分支**，[maskformer_model.py:L246-L250](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L246-L250)：

```python
if self.semantic_on:
    r = retry_if_cuda_oom(self.semantic_inference)(mask_cls_result, mask_pred_result)
    if not self.sem_seg_postprocess_before_inference:
        r = retry_if_cuda_oom(sem_seg_postprocess)(r, image_size, height, width)
    processed_results[-1]["sem_seg"] = r
```

`semantic_inference` 内部的 `softmax`、`sigmoid`、`einsum("qc,qhw->chw")` 各自物化 (Q,K+1)、(Q,H,W)、(K,H,W) 的临时量，全部正比于当前工作尺寸的像素数。

**包裹点 ④：全景分支**，[maskformer_model.py:L253-L255](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L253-L255)：

```python
if self.panoptic_on:
    panoptic_r = retry_if_cuda_oom(self.panoptic_inference)(mask_cls_result, mask_pred_result)
    processed_results[-1]["panoptic_seg"] = panoptic_r
```

u6-l3 精读过：`panoptic_inference` 里有 `cur_prob_masks`（Q 个 H×W 概率图）、`argmax` 的 id 图、逐段的 bool mask，都是全尺寸中间量。

**包裹点 ⑤：实例分支**，[maskformer_model.py:L257-L260](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L257-L260)：

```python
if self.instance_on:
    instance_r = retry_if_cuda_oom(self.instance_inference)(mask_cls_result, mask_pred_result)
    processed_results[-1]["instances"] = instance_r
```

`instance_inference` 的重打分（u6-l2）要对 topk 个 mask 做 `sigmoid` 与展平乘加，同样是全尺寸张量操作。

**一个反差值得注意**：紧邻其上的批量上采样**没有**包裹，[maskformer_model.py:L222-L227](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L222-L227)：

```python
mask_pred_results = F.interpolate(
    mask_pred_results,
    size=(images.tensor.shape[-2], images.tensor.shape[-1]),
    mode="bilinear",
    align_corners=False,
)
```

这一步把 mask 从 1/4 分辨率插值到 **padding 尺寸**（不是原图尺寸），临时量受 `INPUT.MIN_SIZE_TEST` 控制而非用户图片大小，通常在数百 MB 量级，作者选择不设防。（对比：视频版同一步**是**包裹的，见 4.5。）

**统计结论**（本讲实践任务之一）：图像版 `maskformer_model.py` 中 `retry_if_cuda_oom` 共 **1 行 import + 5 行调用**；5 行调用分布在 3 个推理分支（semantic/panoptic/instance）加前后置 `sem_seg_postprocess`。这就是本讲规格中"三处包裹调用点"的准确展开——三大分支，每个分支各自设防。

#### 4.3.4 代码实践

**实践目标**：亲手统计仓库中所有 `retry_if_cuda_oom` 的 import 源与调用点，验证本节的统计数字。

操作步骤：

1. 在仓库根目录执行（grep 的 `-n` 输出行号）：

```bash
grep -n "retry_if_cuda_oom" mask2former/maskformer_model.py
grep -n "retry_if_cuda_oom" mask2former_video/video_maskformer_model.py
grep -rn "retry_if_cuda_oom" mask2former/ mask2former_video/ --include="*.py" | grep -v "utils/memory.py"
```

2. 为每一条命中分类：import 行（`from ... import`）还是调用行（`retry_if_cuda_oom(xxx)(...)` 形态）。

需要观察的现象与预期结果：

- `mask2former/maskformer_model.py`：1 行 import（L14，来自 `detectron2.utils.memory`）+ 5 行调用（L240、L247、L249、L254、L259）。
- `mask2former_video/video_maskformer_model.py`：1 行 import（L19，来自 `.utils.memory` 仓库内副本）+ 2 行调用（L210、L225）。
- 除上述两个文件与 `mask2former_video/utils/memory.py` 自身外，源码目录中**没有**其他使用点（图像包 `mask2former/` 下没有自带 memory 副本）。

#### 4.3.5 小练习与答案

**练习 1**：为什么不把 `retry_if_cuda_oom` 包在 `MaskFormer.forward` 整个函数外面？

**答案**：其一，forward 的参数 `batched_inputs` 是 **list[dict]** 嵌套结构，`maybe_to_cpu` 不递归处理，CPU 降级段无法搬运输入；其二，forward 有状态（self 上的模块），且 backbone/pixel decoder 的 OOM 多半意味着 batch 或基础分辨率设置不当，重试与 CPU 降级都会极慢甚至无法完成；其三，粒度越细，降级波及面越小——只有真正爆掉的那一步搬去 CPU，其余步骤仍在 GPU 上快速执行。

**练习 2**：L243 `mask_cls_result = mask_cls_result.to(mask_pred_result)` 为什么必要？

**答案**：降级发生后 `mask_pred_result` 可能已变成 CPU 张量；若分类 logits 还留在 GPU，下一步 `semantic_inference` 的 `einsum` 会因两个操作数设备不同而报错。`.to(tensor)` 以目标张量的设备与 dtype 为准，把两边对齐。

---

### 4.4 模块四：`sem_seg_postprocess_before_inference`——用流程设计规避 OOM

#### 4.4.1 概念说明

重试是**事后补救**；这一节讲**事前设计**。同一个语义推理目标，存在两条等价的计算顺序：

- **先还原、后融合**（before=True）：先把 Q 个 mask 插值到原图尺寸，再做 softmax/sigmoid/einsum 融合。
- **先融合、后还原**（before=False）：先在 padding 尺寸完成全部融合，最后只把融合结果（K 通道）插值到原图。

数学上两者完全等价（插值是线性的，einsum 也是线性的；softmax/sigmoid 作用在通道维，与空间尺寸无关）。但**显存上差别巨大**：前者的所有中间量都物化在原图尺寸，后者的中间量只占 padding 尺寸。参数 docstring 写得直白（[maskformer_model.py:L60-L63](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L60-L63)）：

> whether to resize the prediction back to original input size before semantic segmentation inference or after. **For high-resolution dataset like Mapillary, resizing predictions before inference will cause OOM error.**

#### 4.4.2 核心流程

两种模式的峰值显存估算。设 Q 个 query、K 个类别、原图 \(H_o \times W_o\)、padding 尺寸 \(H_p \times W_p\)，float32 每元素 4 字节，则仅"Q 张 mask 的 sigmoid 中间量"一项：

\[
\text{before}:\ \text{mem} = Q \cdot H_o \cdot W_o \cdot 4, \qquad
\text{after}:\ \text{mem} = Q \cdot H_p \cdot W_p \cdot 4
\]

代入一组现实数字（Q=100；Mapillary 风格原图 6000×4000；`MIN_SIZE_TEST=800` 时 padding 约 800×1066）：

| 模式 | 该项临时量 | 面积比 |
| --- | --- | --- |
| before | \(100 \times 6000 \times 4000 \times 4 \approx 9.6\) GB | 1× |
| after | \(100 \times 800 \times 1066 \times 4 \approx 0.34\) GB | 约 1/28 |

原图面积是 padding 尺寸的约 28 倍——before 模式仅这一项临时量就逼近 10 GB，叠加 einsum 输出、softmax 副本等必然 OOM；after 模式把所有多通道中间量都压在 padding 尺寸，只在最后为最终输出付一次 K 通道的原图插值。（数字为量级估算，实际峰值还与算子内部实现、其他中间量有关。）

但 before 模式并非多余——**全景与实例分支必须用它**：它们的输出本身就包含"原图尺寸的每个 query 一张 mask"（`panoptic_seg` 逐段写回、`pred_masks` 逐实例挂载），mask 不先还原到原图尺寸，输出就是错的。所以这个开关不能简单全局设 False，而是：

```
最终值 = 配置值 or PANOPTIC_ON or INSTANCE_ON
语义-only 任务 → 可选 False（默认 False，高分辨率救星）
含全景/实例   → 强制 True（正确性要求）
```

#### 4.4.3 源码精读

**默认值注册**，[config.py:L57-L60](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/config.py#L57-L60)：

```python
cfg.MODEL.MASK_FORMER.TEST.PANOPTIC_ON = False
cfg.MODEL.MASK_FORMER.TEST.OBJECT_MASK_THRESHOLD = 0.0
cfg.MODEL.MASK_FORMER.TEST.OVERLAP_THRESHOLD = 0.0
cfg.MODEL.MASK_FORMER.TEST.SEM_SEG_POSTPROCESSING_BEFORE_INFERENCE = False
```

默认 `False`——即"先融合、后还原"的省显存路径；且 `configs/` 下没有任何 yaml 显式覆盖它（可用 `grep -rn SEM_SEG_POSTPROCESSING_BEFORE_INFERENCE configs/` 验证，结果为空），全景/实例配置拿到的 `True` 全部来自下面的运行时逻辑而非 yaml。

**强制改写**，[maskformer_model.py:L149-L153](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L149-L153)：

```python
"sem_seg_postprocess_before_inference": (
    cfg.MODEL.MASK_FORMER.TEST.SEM_SEG_POSTPROCESSING_BEFORE_INFERENCE
    or cfg.MODEL.MASK_FORMER.TEST.PANOPTIC_ON
    or cfg.MODEL.MASK_FORMER.TEST.INSTANCE_ON
),
```

配置值与两个任务开关做**逻辑或**：只要全景或实例开启，无论 yaml 写什么，都强制走 before 路径——正确性优先于省显存。

**构造期断言**，[maskformer_model.py:L93-L94](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L93-L94)：

```python
if not self.semantic_on:
    assert self.sem_seg_postprocess_before_inference
```

反向兜底：若语义分支关闭（模型只做全景/实例），必须前置。原因：after 路径的"最后还原"只存在于语义分支内部（包裹点 ③），语义关闭时没有人负责把尺寸还原到原图，故必须前置。这把"语义-only 可省、其余必须前置"的约束在**构建期**就校验掉，而不是留到推理出错误形状的结果。

#### 4.4.4 代码实践

**实践目标**：写一个"内存账单计算器"，把两种模式的峰值差异算出来，直观理解开关的取舍。

操作步骤（示例代码）：

```python
# 示例代码：before/after 两种推理顺序的显存账单估算
def bill(Q, K, Ho, Wo, Hp, Wp, bytes_per=4):
    before = (Q + K) * Ho * Wo * bytes_per          # sigmoid 副本 + einsum 输出，都在原图尺寸
    after  = (Q + K) * Hp * Wp * bytes_per \
             + K * Ho * Wo * bytes_per              # 中间量在 padding，仅最终输出付原图尺寸
    return before / 1024**3, after / 1024**3

# COCO 全景：Q=100, K=133；网络输入约 800x1333（padding 后）
# 原图取高分辨率数据集量级 6000 x 4000
b, a = bill(Q=100, K=133, Ho=6000, Wo=4000, Hp=800, Wp=1344)
print(f"before 模式峰值 ≈ {b:.1f} GiB | after 模式峰值 ≈ {a:.1f} GiB | 比值 {b/a:.1f}x")
```

需要观察的现象与预期结果：

- before 约 40 GiB 量级、after 约 13 GiB 量级（含最终 K 通道输出的不可省部分），比值约 3 倍——即使是"必须付的最终输出"占大头时，把**中间量**压到 padding 尺寸仍显著降低峰值；若只比中间量（4.2 节的 9.6 GB vs 0.34 GB），差距是 28 倍。
- 把 `Ho, Wo` 换成 1920×1080 的普通图片再跑一次，观察两种模式差距缩小——说明该开关是**专为高分辨率数据集**设计的（Mapillary），普通图片上两种模式都安全。
- 结果为估算推演（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：一个只做语义分割（`SEMANTIC_ON=True`，另外两开关 False）的 Mapillary 高分辨率配置，`sem_seg_postprocess_before_inference` 最终是 True 还是 False？为什么这样设计？

**答案**：False（配置默认值，无 or 改写）。语义分支的输出是 K 通道概率图，融合可以在低分辨率完成后一次性上采样，中间量省 28 倍；这正是 docstring 点名 Mapillary 的原因。

**练习 2**：既然 after 模式省显存，为什么全景/实例不也改成 after？

**答案**：全景的 `panoptic_seg` 是逐像素 id 图、实例的 `pred_masks` 是逐实例 mask，二者的**输出本身就是全尺寸 mask**，逐 query 的 mask 必须先还原到原图尺寸才能做 argmax 分配/挂载；after 模式只在语义分支有"最后统一上采样"的等价变换可用。正确性约束压倒显存优化，故 from_config 用 `or` 强制前置。

**练习 3**：L93-94 的断言在什么配置组合下会触发？

**答案**：`SEMANTIC_ON=False` 且 `SEM_SEG_POSTPROCESSING_BEFORE_INFERENCE=False` 且 `PANOPTIC_ON=False` 且 `INSTANCE_ON=False`——但这个组合意味着三个任务全关，本身无意义。实际上由于 L149-153 的 or 逻辑，只要全景或实例任一开启就会把 before 置 True，断言防的是"三开关全关还显式写 False"这类自相矛盾的配置，把错误拦在构建期。

---

### 4.5 模块五：视频版的用法差异——仓库内副本的消费现场

#### 4.5.1 概念说明

u9-l1 讲过视频包是"薄层复用"，但 `retry_if_cuda_oom` 是个**显式例外**：视频包没有去 import detectron2 的工具，而是自带了一份副本（`mask2former_video/utils/memory.py`），并由 `video_maskformer_model.py` 消费。更有意思的是**包裹位置的不同**，它折射出图像与视频推理的显存压力差异。

#### 4.5.2 核心流程

视频推理的显存压力源：视频版是**整段视频一次前向**（u9-l1），mask 张量形状为 (Q, T, H, W)——比图像版多一个时间维 T，任何全尺寸操作的开销都随 T 线性放大。

```
图像版:  F.interpolate（→ padding 尺寸）【无包装】 → 逐分支包裹
视频版:  retry_if_cuda_oom(F.interpolate)（→ padding 尺寸）【有包装】
         → retry_if_cuda_oom(self.inference_video)（整个推理一步包裹）
```

#### 4.5.3 源码精读

**副本的导入**，[video_maskformer_model.py:L19](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L19)：

```python
from .utils.memory import retry_if_cuda_oom
```

**包裹点 ⑥：带重试的上采样**，[video_maskformer_model.py:L210-L215](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L210-L215)：

```python
mask_pred_result = retry_if_cuda_oom(F.interpolate)(
    mask_pred_results[0],
    size=(images.tensor.shape[-2], images.tensor.shape[-1]),
    mode="bilinear",
    align_corners=False,
)
```

对比图像版 [maskformer_model.py:L222-L227](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former/maskformer_model.py#L222-L227) 的**裸** `F.interpolate`：同样是"1/4 → padding 尺寸"的插值，视频版包了、图像版没包。合理推断：视频版插值的输入是 (Q, T, H/4, W/4)，输出 (Q, T, H_pad, W_pad)，元素数是图像版的 T 倍，长视频（T 帧数大）时这一步本身就能 OOM；图像版同一步只有 (B·Q) 张，padding 尺寸可控。此处推断以两个文件的代码差异为准。

**包裹点 ⑦：整个视频推理**，[video_maskformer_model.py:L225](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L225)：

```python
return retry_if_cuda_oom(self.inference_video)(mask_cls_result, mask_pred_result, image_size, height, width)
```

图像版把三个分支各自包裹（细粒度、单分支降级）；视频版只有一个 `inference_video`（u9-l1 讲过它硬编码 topk(10)、不分三开关），于是整体包裹（粗粒度）。粒度差异的代价：视频版一旦 OOM，**整段推理**搬去 CPU，而图像版只搬当前分支。

顺带一提：`F.interpolate`、`sem_seg_postprocess` 都是纯函数，满足 4.2 节的无状态要求；`self.inference_video` 虽是绑定方法，但不修改 self 状态，同样满足。

#### 4.5.4 代码实践

**实践目标**：对比两版模型在同一步（mask 上采样）上的防御差异，并动手体验"粒度"对降级范围的影响。

操作步骤：

1. 用 4.2.4 的 `flaky_upsample` 实验代码，把被包裹函数改成一个内部依次执行"融合 + argmax"两步的组合函数：

```python
# 示例代码：粗粒度包裹 —— 一步 OOM 则两步全部走 CPU
def two_step_inference(mask_cls, mask_pred):
    fused = torch.einsum("qc,qhw->chw", mask_cls.softmax(-1)[..., :-1], mask_pred.sigmoid())
    ids = fused.argmax(0)
    return fused, ids

# 第一次调用在 einsum 处抛模拟 OOM，观察 argmax 是否也随第三次重试走到 CPU
```

2. 再把 `retry_if_cuda_oom` 分别只包 `einsum` 那一步，重复实验。

需要观察的现象与预期结果：粗粒度版本 OOM 后整段计算搬到 CPU；细粒度版本只有 `einsum` 在 CPU 执行，`argmax` 仍在原设备。这正是图像版（三分支各包各的）与视频版（整体一包）的策略差。结果为推演（待本地验证）。

#### 4.5.5 小练习与答案

**练习 1**：仓库内 `mask2former_video/utils/memory.py` 这份副本，在整个仓库里被谁消费？

**答案**：只有 [video_maskformer_model.py:L19](https://github.com/facebookresearch/Mask2Former/blob/9b0651c6c1d5b3af2e6da0589b719c514ec0d69a/mask2former_video/video_maskformer_model.py#L19) 一个消费方（L210、L225 两处调用）；图像版模型用的是 detectron2 的同名工具。`mask2former_video/utils/__init__.py` 甚至没有导出它，必须写全路径 import。

**练习 2**：视频版把整个 `inference_video` 包进去，降级时会发生什么图像版不会发生的事？

**答案**：整个推理函数的所有中间步骤（topk 选 query、逐帧 mask 处理等）都在 CPU 上以 float32 执行，返回结果也整体落在 CPU；而图像版最多只有一个分支（如 `panoptic_inference`）搬去 CPU，语义/实例分支若未 OOM 仍在 GPU 上。粗粒度换来了代码简洁，代价是降级波及面大。

---

## 5. 综合实践

**任务：给你的推理脚本装上"三段降级 + 显存账单"双重防线。**

把本讲内容串起来，写一个可直接放进自己项目里的小工具（示例代码）：

```python
# 示例代码：oom_guard.py —— 显存账单 + 三段降级的组合演练
import logging
import torch
from mask2former_video.utils.memory import retry_if_cuda_oom   # 只依赖 torch，无需 detectron2

logging.basicConfig(level=logging.INFO)

def memory_bill(Q, K, Ho, Wo, Hp, Wp, bytes_per=4):
    """4.4 节的账单函数：返回 before/after 两种推理顺序的估算峰值 (GiB)"""
    before = (Q + K) * Ho * Wo * bytes_per / 1024**3
    after = ((Q + K) * Hp * Wp * bytes_per + K * Ho * Wo * bytes_per) / 1024**3
    return before, after

def panoptic_style_postprocess(mask_pred, mask_cls, orig_hw):
    """玩具版全景后处理：融合 -> 逐像素 argmax，全是顶层张量、无状态"""
    fused = torch.einsum("qc,qhw->chw", mask_cls.softmax(-1)[..., :-1], mask_pred.sigmoid())
    return fused.argmax(0)

def main():
    # 第 1 步：先算账单，决定是否需要在 GPU 上跑
    Q, K = 100, 133
    b, a = memory_bill(Q, K, Ho=6000, Wo=4000, Hp=800, Wp=1344)
    print(f"账单: before={b:.1f} GiB, after={a:.1f} GiB —— 超过显存就该考虑 after 顺序或 CPU")

    # 第 2 步：无论如何，给后处理装上三段降级
    guarded = retry_if_cuda_oom(panoptic_style_postprocess)
    mask_pred = torch.randn(Q, 800 // 4, 1344 // 4)          # 实际使用时来自模型输出（GPU 张量）
    mask_cls = torch.randn(Q, K + 1)
    result = guarded(mask_pred, mask_cls, (6000, 4000))
    print("输出形状:", tuple(result.shape), "设备:", result.device)

if __name__ == "__main__":
    main()
```

完成后的检查清单：

1. 账单函数复现 4.4.4 的数字（before ≈ 40 GiB、after ≈ 13 GiB 量级）。
2. CPU 环境下脚本正常跑完，输出设备为 `cpu`（模拟了"第 3 段降级成功后结果在 CPU"的情形）。
3. 有 GPU 的机器上，把 `mask_pred` 换成巨大的 CUDA 张量（例如 `torch.randn(100, 2048, 2048, device="cuda")`）逼出真实 OOM，观察日志顺序：无日志（第 1 段成功）→ 仅 OOM traceback 被吞后重试 → `Attempting to copy inputs to CPU due to CUDA OOM`。
4. 对照源码核对：降级成功后返回的 `result` 是否如 docstring 警告的那样落在 CPU（"user's responsibility"）。

（本环境无 GPU 且未执行，全部为源码推演，待本地验证。）

## 6. 本讲小结

- `retry_if_cuda_oom` 的三段降级：**直接执行 → `torch.cuda.empty_cache()` 清碎片重试 → 参数逐个搬 CPU（转 float32、关 autocast）重试**；第三段不再吞异常，失败即向上抛。
- `_ignore_torch_cuda_oom` 是只认 `"CUDA out of memory. "`（带尾随空格）字符串的异常过滤器；`return` 写在 `with` 块内造就"异常即控制流"，字符串匹配的脆弱性由作者注释自认，失配时保守地退化为不重试。
- 被包裹函数的两条硬约束：**无状态**（最多被调 3 次）、**参数是顶层张量**（`maybe_to_cpu` 不递归嵌套结构）；返回值可能落在 CPU，由调用方负责。
- 图像版 `maskformer_model.py` 共 **5 个调用行**覆盖 3 大推理分支（semantic/panoptic/instance）与前后置 `sem_seg_postprocess`，import 自 detectron2；视频版 2 个调用行（`F.interpolate` 与整个 `inference_video`），消费的是仓库内副本 `mask2former_video/utils/memory.py`——粗粒度整体包裹 vs 细粒度分支包裹。
- `sem_seg_postprocess_before_inference` 是**事前的流程级 OOM 规避**：语义-only 任务默认 False（中间量压在 padding 尺寸，Mapillary 这类高分辨率数据集省约 28 倍），全景/实例经 from_config 的 `or` 强制 True（正确性要求），语义关闭时构造期断言兜底。

## 7. 下一步学习建议

- 下一讲 u10-l3（测试时增强 SemanticSegmentorWithTTA）会看到推理路径的另一种包装：TTA 对同一张图生成多个增强版本逐一推理，显存压力同样随版本数放大，可与本讲的降级机制对照阅读。
- 建议回到 detectron2 源码读 `detectron2/utils/memory.py` 与 `detectron2/modeling/postprocessing.py` 中的 `sem_seg_postprocess`，对照确认两份 `retry_if_cuda_oom` 的实现一致性，以及包裹点 ①③ 所包函数的内部细节（crop padding → interpolate 两步）。
- 若想继续深挖显存主题，可结合 u10-l1（FLOPs 与激活量分析）理解"峰值激活显存"与"计算量"的区别：`retry_if_cuda_oom` 救的是激活与临时量，而不是参数量。

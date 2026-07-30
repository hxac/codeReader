# Python 侧迁移：device 与运行时接口替换

## 1. 本讲目标

本讲是「从 GPU 迁移到 Ascend NPU」单元的第一讲，目标只有一个：**让你手上那段写着 `device='cuda'`、`torch.cuda.*` 的 GPU 版 Triton 脚本，在 Ascend NPU 上跑起来、并且结果正确**。

学完后你应该能够：

- 说清楚 `torch_npu` 这个库在迁移中扮演的「适配层」角色，以及为什么它必须被 `import`。
- 把脚本里所有形式的 GPU 设备声明（`device='cuda'`、`.cuda()`、`.to('cuda')`、`torch.device('cuda', id)`）系统性地替换成对应的 NPU 形式。
- 把 `torch.cuda.*` 一类的运行时/同步接口（`current_device`、`synchronize`、Stream/Event）映射到 `torch.npu.*`，并知道哪些 GPU 专属逻辑应该直接删掉。
- 牢记迁移的第一原则：**先保持 `@triton.jit` 的 kernel body 不动，只改宿主侧（host side）的 device/运行时代码，优先验证正确性**，性能问题留到后面几讲。

本讲只动 Python 宿主侧，**不碰 kernel 内部**。涉及 grid 核心分配、内存对齐、UB 溢出等更深的迁移话题，会在 u2-l2、u2-l3 展开。

## 2. 前置知识

在开始前，请确认你已经具备以下认知（这些在 u1 系列讲义中已建立）：

- **Triton-Ascend 是社区 Triton 的「昇腾 NPU 后端」**：上游 Triton 把 `@triton.jit` 标注的 Python kernel 编译成与硬件无关的 TTIR，Triton-Ascend 接手把它变换成能在 NPU 上跑的二进制（见 u1-l1）。
- **kernel body 是目标无关的**：你在 u1-l4 跑通的 `01-vector-add.py` 里，`tl.load` / `tl.store` / `tl.program_id` 这些写法和 GPU 版完全一致，唯一不同的只是张量住在哪个设备上。
- **device 的概念**：PyTorch 里每个张量都有一个 `device` 属性，标明它住在哪里（CPU、CUDA 或 NPU）。kernel 启动时，传入的指针必须指向「当前后端认识的设备」。
- **运行时同步**：GPU 上异步发起的 kernel 需要用 `torch.cuda.synchronize()` 等待真正执行完毕，才能安全读取结果；NPU 同样是异步模型。

如果你对「为什么 `device='npu'` 就能让 Triton-Ascend 接管编译」还有疑问，回顾 u1-l4 的 vector-add 例子：那是一份已经迁移好的 NPU 脚本，本讲要讲的就是「从 GPU 版到它」这段替换过程。

## 3. 本讲源码地图

本讲涉及的文件都在「迁移」这条线上，分两类：

| 文件 | 作用 |
| --- | --- |
| [docs/en/migration_guide/migrate_from_gpu.md](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md) | 官方迁移总指南，给出「五步迁移法」与若干完整 diff 示例。本讲的理论骨架。 |
| [docs/en/quick_start.md](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/quick_start.md) | 快速入门，含一份从 GPU `test_add.py` 到 NPU 的最小迁移示例和接口映射表。 |
| [third_party/ascend/tutorials/01-vector-add.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/01-vector-add.py) | 已迁移好的 NPU 版 vector-add，可作为「迁移后长什么样」的参照。 |
| [third_party/ascend/backend/driver.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py) | Triton-Ascend 运行时驱动，内部如何把「当前设备」解析成 `torch.device("npu", ...)`。 |
| [third_party/ascend/backend/backend_register.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/backend_register.py) | 后端策略注册表，把 `torch.npu.current_device()` 等接口注册给 torch_npu 后端。 |
| [third_party/ascend/backend/testing.py](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/testing.py) | 项目自带的性能测试工具，真实代码里 `torch.npu.synchronize()` 的用法范例。 |

后三个 `.py` 不是给用户写的迁移脚本，而是 **Triton-Ascend 自身** 是怎么调用 `torch.npu.*` 的——读它们能让你确认「这套替换不是约定俗成的口诀，而是项目代码里实实在在的调用」。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，前三个对应迁移要替换的三类东西（`torch_npu` 库、设备声明、运行时接口），第四个把它们串成一套可执行的总流程。

### 4.1 torch_npu：让 PyTorch 认识昇腾 NPU

#### 4.1.1 概念说明

PyTorch 原生只认识 CPU 和 CUDA 两类设备。你在脚本里写 `device='npu'`、调用 `torch.npu.current_device()`，这些 API 在纯 PyTorch 里**根本不存在**——它们来自华为提供的适配库 **`torch_npu`**。

可以把 `torch_npu` 理解成一座桥：

- 它在 `import` 时向 PyTorch 注册了一个名为 `npu` 的新设备后端；
- 注册之后，`torch.npu` 这个子模块（类似 `torch.cuda`）、张量的 `.npu()` 方法、`device='npu'` 字符串才全部变得可用；
- 它还把 CANN 运行时（stream、event、profiler 等）包装成和 `torch.cuda` 几乎一致的接口。

**所以迁移的第一步永远是 `import torch_npu`。** 没有这一行，后面所有的 `npu` 替换都会抛 `AttributeError`。这也正是官方迁移指南把「Add `import torch_npu`」列为五步法第一步的原因。

一个常被忽略的细节：`import torch_npu` 通常要写在 `import triton` 之前或之后紧邻位置，确保 Triton-Ascend 后端在初始化、探测设备时 `torch.npu` 已经就绪。

#### 4.1.2 核心流程

torch_npu 生效的流程非常线性：

1. 脚本顶部 `import torch` 后，紧接着 `import torch_npu`。
2. torch_npu 完成注册，`torch.npu` 命名空间可用。
3. 之后所有 `device='npu'`、`.npu()`、`torch.npu.*` 调用才合法。
4. Triton-Ascend 的驱动在探测设备时，也是经由 `torch.npu.current_device()` 拿到当前 NPU 卡号——和我们用户脚本走的是同一套接口。

```
import torch          # 基础张量库
import torch_npu      # ← 注册 npu 设备，桥接 CANN
import triton         # 后端在此之后能正确探测到 npu
```

#### 4.1.3 源码精读

官方迁移指南五步法的第一步就是导入 torch_npu：

[docs/en/migration_guide/migrate_from_gpu.md:10-10](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L10-L10) —— 「1. Add `import torch_npu` to the Python file.」，明确要求迁移的第一动作。

已经迁移好的 NPU 版 vector-add 教程，顶部正是这样写的：

[third_party/ascend/tutorials/01-vector-add.py:43-43](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/01-vector-add.py#L43-L43) —— `import torch_npu`，紧接在 `import torch` 之后。正是因为有了它，第 97–98 行的 `device='npu'` 才能生效。

版本上，`torch_npu` 与 Python、CANN 是绑定的。快速入门里给出的匹配版本是权威参考：

[docs/en/quick_start.md:27-27](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/quick_start.md#L27-L27) —— 「The currently matched torch_npu version is 2.7.1.post4.」。版本不匹配会导致 `import torch_npu` 失败或运行时崩溃，环境问题在 u1-l3 已详细讨论。

> 提示：`import torch_npu` 本身不产生可见输出，但它是「开灯」的那一下——后面的所有 NPU 调用都依赖它。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「没有 torch_npu，npu 设备就不存在」。

**操作步骤**：

1. 在已装好 triton-ascend 的环境里，打开一个 Python 交互终端。
2. 先执行：
   ```python
   import torch
   print(torch.npu.current_device())   # 预期：抛 AttributeError
   ```
3. 再执行：
   ```python
   import torch_npu
   print(torch.npu.current_device())   # 预期：打印当前 NPU 卡号，如 0
   ```

**需要观察的现象**：第一步会报 `module 'torch' has no attribute 'npu'`（或类似），第二步才正常返回卡号。

**预期结果**：这一对比直观说明 torch_npu 的「注册」作用——它不是工具函数集合，而是给 PyTorch 装上了 npu 这个设备后端。

> 是否能跑通取决于本机是否有 NPU 卡与 CANN 环境，**待本地验证**；在没有硬件的机器上，第二步 `import torch_npu` 仍可成功，但 `current_device()` 会因找不到设备而报错，同样能佐证「适配层」的存在。

#### 4.1.5 小练习与答案

**练习 1**：为什么迁移脚本里「删掉 `import torch_npu`」会让所有 `device='npu'` 同时失效？

**参考答案**：`device='npu'` 依赖 PyTorch 认识 `npu` 这个设备类型，而该类型是由 `torch_npu` 在 import 时注册的。删掉导入，`torch.npu` 命名空间和 `npu` 设备一起消失，于是 `device='npu'` 在创建张量时就找不到对应后端。

**练习 2**：`import torch_npu` 应该放在脚本的什么位置？放在文件最末尾行不行？

**参考答案**：应放在 `import torch` 之后、**任何使用 `device='npu'` / `torch.npu.*` / Triton kernel 启动之前**。放在末尾会导致前面的 npu 调用或 Triton 后端设备探测拿不到 npu 设备而报错。

---

### 4.2 device 声明替换

#### 4.2.1 概念说明

GPU 脚本里「张量住在哪」是用**设备声明**表达的，散落在好几处、有好几种写法。迁移的核心动作就是把所有 `cuda` 设备声明改成 `npu`。常见的四种形态：

| GPU 写法 | NPU 写法 | 出现场景 |
| --- | --- | --- |
| `device='cuda'` / `device="cuda"` | `device='npu'` / `device="npu"` | `torch.randn(..., device=...)` 等张量构造 |
| `tensor.cuda()` | `tensor.npu()` | 把 CPU 张量搬到设备 |
| `tensor.to('cuda')` / `.to("cuda")` | `tensor.to('npu')` / `.to("npu")` | 跨设备搬运，保留 dtype |
| `torch.device('cuda', idx)` | `torch.device('npu', idx)` | 显式构造设备对象 |

关键直觉：**`npu` 就是 `cuda` 的镜像**。torch_npu 刻意让两者的 API 形状一致，所以这里的替换几乎是「字面替换 `cuda` → `npu`」。

#### 4.2.2 核心流程

官方快速入门把这套替换总结成一张映射表：

```
device='cuda'              →  device='npu'
tensor.cuda()              →  tensor.npu()
torch.cuda.current_device()→  torch.npu.current_device()
torch.cuda.synchronize()   →  torch.npu.synchronize()
```

迁移时按下面的顺序扫一遍脚本即可（其中后两项属于运行时接口，下一节细讲）：

1. 全文搜索 `cuda`（注意区分大小写、留意引号风格 `'` 与 `"`）。
2. 把设备字符串 `'cuda'`/`"cuda"` 改成 `'npu'`/`"npu"`。
3. 把 `.cuda()` 方法调用改成 `.npu()`。
4. 把 `.to('cuda')` 改成 `.to('npu')`。
5. 把 `torch.device('cuda', ...)` 改成 `torch.device('npu', ...)`。
6. **不要**误改 kernel body 里的东西——kernel 里通常没有 `cuda` 字样（它是目标无关的 TTIR）。

> 易错点：`BLOCK_SIZE`、`cdiv`、`program_id` 这些都在 kernel 内部，与设备无关，**一律不动**。

#### 4.2.3 源码精读

官方映射表来自快速入门的「Example 2」：

[docs/en/quick_start.md:117-122](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/quick_start.md#L117-L122) —— 四行替换规则，覆盖了最常见的 `device=`、`.cuda()`、`current_device`、`synchronize`。这张表是本节和下一节的「总纲」。

同一份文档给出了对应的最小 diff，可以清楚看到只动了设备声明：

[docs/en/quick_start.md:143-150](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/quick_start.md#L143-L150) —— 把 `torch.randn(SIZE, device='cuda', ...)` 改成 `device='npu'`，把 `output_cpu.cuda()` 改成 `output_cpu.npu()`。注意被标注 `# ...(kernel code remains unchanged)...` 的 kernel 部分**一行都没改**。

迁移指南五步法的第二步，正式定义了这条替换规则的覆盖范围：

[docs/en/migration_guide/migrate_from_gpu.md:11-11](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L11-L11) —— 「Find `device="cuda"`, `device='cuda'`, `.cuda()`, `.to("cuda")`, and similar device specifications, and change them to `device="npu"`, `device='npu'`, `.npu()`, or `.to("npu")`.」

迁移完成后的样子，直接看 vector-add 教程：

[third_party/ascend/tutorials/01-vector-add.py:97-98](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/tutorials/01-vector-add.py#L97-L98) —— `x = torch.rand(size, device='npu')` 与 `y = torch.rand(size, device='npu')`，这就是 GPU 版 `device='cuda'` 替换后的最终形态。

#### 4.2.4 代码实践

**实践目标**：把一段 GPU 设备声明改成 NPU，并核对替换是否完整。

**操作步骤**：

1. 取下面这段「全是 GPU 设备声明」的片段（示例代码，提炼自官方 GPU 版 `test_add.py`）：

   ```python
   # 示例代码：GPU 版设备声明片段
   device_id = torch.cuda.current_device()
   device = torch.device('cuda', device_id)
   x = torch.randn(SIZE, device='cuda', dtype=torch.float32)
   y = torch.empty(SIZE, dtype=torch.float32).cuda()
   z = y.to('cuda')
   ```

2. 按本节的映射规则，逐行替换为 NPU 版本。
3. 用编辑器的「全文搜索 `cuda`」复核，确保没有漏网（注意 `device_id`、`cdiv` 这类**不含** `cuda` 的标识符不要误改）。

**需要观察的现象**：替换后，脚本里应当**再也搜不到**作为设备名出现的 `cuda`；而 `torch.cuda.current_device()` 这一行属于运行时接口，会在下一节处理。

**预期结果**：得到对应的 NPU 版：
```python
device_id = torch.npu.current_device()   # 4.3 节处理
device = torch.device('npu', device_id)
x = torch.randn(SIZE, device='npu', dtype=torch.float32)
y = torch.empty(SIZE, dtype=torch.float32).npu()
z = y.to('npu')
```

> 命令是否可执行需 NPU 环境，**待本地验证**；但「替换是否完整」这一步纯靠文本检查即可完成。

#### 4.2.5 小练习与答案

**练习 1**：下面这行迁移后哪里错了？`x = torch.rand(size, device='npu', dtype=torch.float32).cuda()`

**参考答案**：前半段已经把设备声明改成了 `device='npu'`，但末尾又调用了 `.cuda()`，等于把刚建在 NPU 上的张量再搬回（不存在的）CUDA 设备，会报错。应改为 `.npu()` 或干脆删掉（因为 `device='npu'` 已经把它放在 NPU 上了）。这说明替换必须**全文一致**，不能只改一处。

**练习 2**：为什么 kernel 内部的 `BLOCK_SIZE: tl.constexpr` 在迁移时不需要改？

**参考答案**：`BLOCK_SIZE` 是 kernel 内的编译期常量，描述每个 program 处理多少元素，与张量住在哪个设备无关；kernel body 被编译成目标无关的 TTIR，由后端接管，因此不在 device 声明替换的范围内。

---

### 4.3 运行时同步接口替换

#### 4.3.1 概念说明

GPU 脚本里除了「张量住哪」，还有一类**运行时控制接口**挂在 `torch.cuda.*` 下，用来管理设备、流（stream）、事件（event）和同步。NPU 上它们对应 `torch.npu.*`，接口形状几乎一样。本节处理迁移指南五步法中的第 3、4 步。

常见映射：

| GPU 接口 | NPU 接口 | 用途 |
| --- | --- | --- |
| `torch.cuda.current_device()` | `torch.npu.current_device()` | 查询当前设备卡号 |
| `torch.cuda.synchronize()` | `torch.npu.synchronize()` | 等待当前设备上所有异步任务完成 |
| `torch.cuda.set_device(id)` | `torch.npu.set_device(id)` | 切换当前设备 |
| `torch.cuda.Stream` / `Event` | `torch.npu.Stream` / `Event` | 流与事件（多数场景可删） |

有两点特别要注意：

1. **「替换 or 删除」要分清**：`current_device`、`synchronize` 这类是功能性的，直接替换；而 GPU 脚本里一些**仅用于 CUDA 设备发现/校验**的逻辑（典型是 `assert ... == triton.runtime.driver.active.get_active_torch_device()`），在 NPU 上要么多余、要么语义不同，应**直接删除**。
2. **同步语义不变**：NPU 和 GPU 一样是异步执行模型——kernel 发起后立即返回，必须 `synchronize()` 后读取结果才可靠。这一点在 u1-l4 的 vector-add 里因为脚本自然结束而没显式体现，但写测试/benchmark 时必须有。

#### 4.3.2 核心流程

一个典型的「GPU 测试函数」迁移后，运行时部分长这样：

```
# GPU 版
device_id = torch.cuda.current_device()
torch.cuda.synchronize()        # 等 kernel 跑完再比对

# NPU 版
device_id = torch.npu.current_device()
torch.npu.synchronize()         # 同样的等待语义
```

而下面这种 GPU 专属逻辑应当**删除**，而不是替换：

```
# GPU 版（删除，不替换）
DEVICE = triton.runtime.driver.active.get_active_torch_device()
assert x.device == DEVICE and y.device == DEVICE
```

> 为什么删？因为在 NPU 后端里，`get_active_torch_device()` 返回的是 `torch.device("npu", ...)`（见下面的源码），这种「显式断言设备一致性」的写法是 GPU 脚本的习惯，NPU 上由后端自行保证，强行保留反而容易因写法差异误报。

#### 4.3.3 源码精读

迁移指南第 3、4 步明确区分了「替换」与「删除」：

[docs/en/migration_guide/migrate_from_gpu.md:12-13](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L12-L13) —— 第 3 步要求把 `torch.cuda.*`、CUDA stream/event/sync「替换为 NPU 对应接口或删除多余同步逻辑」；第 4 步要求删除「仅用于 GPU 设备发现的逻辑，例如围绕 `triton.runtime.driver.active.get_active_torch_device()` 的断言」。

**为什么 `get_active_torch_device()` 在 NPU 上是 `torch.device("npu", ...)`？** 直接看驱动实现：

[third_party/ascend/backend/driver.py:243-245](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/driver.py#L243-L245) —— `get_active_torch_device` 返回 `torch.device("npu", self.get_current_device())`，即「npu + 当前卡号」。这就是 NPU 后端对「当前活跃设备」的定义。

而 `get_current_device()` 最终调到 `torch.npu.current_device()`，这件事在策略注册表里写得很清楚：

[third_party/ascend/backend/backend_register.py:241-245](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/backend_register.py#L241-L245) —— torch_npu 后端的 `get_current_device` 就是 `return torch.npu.current_device()`。换言之，**Triton-Ascend 自身探测设备用的接口，和我们要用户脚本迁移到的接口，是同一个 `torch.npu.current_device()`**。这印证了「替换是真实存在的、不是口诀」。

设置设备同理：

[third_party/ascend/backend/backend_register.py:254-258](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/backend_register.py#L254-L258) —— torch_npu 后端的 `set_current_device` 调 `torch.npu.set_device(device_id)`，对应 GPU 的 `torch.cuda.set_device`。

至于 `synchronize()`，项目自带的性能测试工具就是用 `torch.npu.synchronize()` 来「shake out of any npu error」（把异步错误逼出来）的：

[third_party/ascend/backend/testing.py:60-60](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/testing.py#L60-L60) —— warmup 每个 kernel 后 `torch.npu.synchronize()`；

[third_party/ascend/backend/testing.py:83-83](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/testing.py#L83-L83) —— 注释直接写 `# shake out of any npu error`，说明同步的另一层作用是让之前异步发起的 NPU 错误及时抛出。这是项目代码里 `torch.npu.synchronize()` 的真实用法范例。

#### 4.3.4 代码实践

**实践目标**：体会「同步」在异步模型里的必要性——漏掉 `synchronize()` 会读到尚未完成的结果。

**操作步骤**（源码阅读 + 改造型实践）：

1. 阅读 [third_party/ascend/backend/testing.py:57-60](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/third_party/ascend/backend/testing.py#L57-L60)，注意它在每次 warmup kernel 之后立刻 `torch.npu.synchronize()`。
2. 在 u1-l4 跑通的 vector-add 脚本基础上，写一个最小对照（示例代码）：
   ```python
   # 示例代码：对比「有/无 synchronize」
   output_triton = add(x, y)
   # 情形 A：不调用 synchronize，立刻读
   diff_no_sync = torch.max(torch.abs(output_torch - output_triton)).item()
   torch.npu.synchronize()   # 情形 B：同步后再读
   diff_synced = torch.max(torch.abs(output_torch - output_triton)).item()
   print(diff_no_sync, diff_synced)
   ```

**需要观察的现象**：情形 A（不同步就读）可能读到未完成的数据，差值偏大或读到的是旧值；情形 B（同步后读）差值应为 0。

**预期结果**：这印证了 NPU 是异步执行——`add(x, y)` 返回时 kernel 未必跑完，必须 `torch.npu.synchronize()` 后再读取。

> 实际数值**待本地验证**（取决于驱动调度，有时情形 A 也碰巧读到正确值，但这不可依赖）。

#### 4.3.5 小练习与答案

**练习 1**：迁移时遇到 `assert x.device == triton.runtime.driver.active.get_active_torch_device()`，应该替换成 `assert x.device == torch.device('npu', 0)` 吗？

**参考答案**：不建议。迁移指南第 4 步明确要求**删除**这类 GPU 设备发现/校验逻辑，而不是替换。原因是 NPU 后端已自行保证设备一致性，且 `get_active_torch_device()` 在 NPU 上返回 `torch.device("npu", current_device())`，硬编码卡号 `0` 反而可能在多卡场景下出错。直接删掉这段断言即可。

**练习 2**：为什么 benchmark/warmup 代码里到处都是 `torch.npu.synchronize()`？

**参考答案**：NPU kernel 异步发起，`synchronize()` 起两个作用——(1) 计时场景下确保上一轮真正结束才开始下一轮，计时才准确；(2) 把异步执行中产生的 NPU 错误「逼」出来及时抛出（testing.py 的注释 `shake out of any npu error` 即此意），避免错误被掩盖到后续难以定位。

---

### 4.4 迁移总流程：五步法与「先验正确性」原则

#### 4.4.1 概念说明

把前三节拼起来，就是官方迁移指南给出的**五步法**。其中最容易被新手忽略、但也最重要的，是第 5 步表达的原则：

> **先保持 `@triton.jit` 的 kernel body 不变，用 NPU 张量验证编译通过和结果正确；性能问题以后再说。**

为什么这条原则重要？

- **隔离变量**：kernel body 是目标无关的 TTIR，理论上不该为了「迁移」而改。如果你一边改 device、一边改 kernel，一旦结果错了，你无法判断是 device 没换干净还是 kernel 改坏了。
- **优先正确，其次性能**：一个跑得快但算错的 kernel 毫无价值。先保证和 PyTorch 参考实现逐元素对齐（像 u1-l4 那样最大误差为 0），再谈调优——调优是 u9 单元的事。
- **后续深水区留给专门讲义**：grid 核心分配（u2-l2）、对齐与 UB（u2-l3）这些真正需要动 kernel/配置的话题，都建立在「正确性已验证」的前提上。

#### 4.4.2 核心流程

五步法（对应迁移指南的同名小节）：

1. `import torch_npu`（4.1 节）。
2. 设备声明 `cuda → npu`（4.2 节）。
3. `torch.cuda.*` 运行时接口替换或删除（4.3 节）。
4. 删除仅用于 GPU 设备发现的逻辑，如 `get_active_torch_device()` 断言（4.3 节）。
5. **保持 kernel body 不变**，用 NPU 张量验证编译与正确性（本节）。

用伪代码描述一次完整迁移的「改动分布」：

```
host 侧（宿主 Python）：       ← 1~4 步都在这里
    import torch_npu            ← 新增
    device='cuda' → 'npu'       ← 改
    .cuda() → .npu()            ← 改
    torch.cuda.* → torch.npu.*  ← 改或删
    删除 GPU 设备发现断言         ← 删

kernel 侧（@triton.jit 内）：   ← 第 5 步：暂不动
    tl.load / tl.store / program_id / cdiv ...  保持原样
```

#### 4.4.3 源码精读

五步法的权威出处：

[docs/en/migration_guide/migrate_from_gpu.md:7-14](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L7-L14) —— 「Migrate Python-Side Device and Runtime Interfaces」整节，第 1–5 步依次对应本讲的四个模块。注意第 14 行（第 5 步）：「Keep the Triton kernel body unchanged at first, and use NPU tensors to verify compilation and correctness.」

一个完整、可信的迁移范本是迁移指南的 Example 1，它把四种改动集中在一个 diff 里：

[docs/en/migration_guide/migrate_from_gpu.md:50-99](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L50-L99) —— 「Complete Vector Addition Migration」。在这个 diff 里你能同时看到：

- 第 54 行：`+import torch_npu`（4.1）；
- 第 58 行：`-DEVICE = triton.runtime.driver.active.get_active_torch_device()`（4.3 删除）；
- 第 79 行：`-assert x.device == DEVICE and ...`（4.3 删除）；
- 第 87–90 行：`device='cuda'` → `device='npu'`（4.2）；
- **第 60–75 行的 kernel body：一个字符都没改**（4.4 第 5 步）。

这正是「先验正确性」原则的最直观体现：所有改动都在宿主侧，kernel 原封不动。

#### 4.4.4 代码实践

**实践目标**：通过阅读一份真实 diff，把每处改动归类到五步法的某一步，并确认 kernel body 零改动。

**操作步骤**：

1. 打开 [docs/en/migration_guide/migrate_from_gpu.md:50-99](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/migration_guide/migrate_from_gpu.md#L50-L99)。
2. 逐行查看 diff，建立一张分类表：每一行 `+`/`-` 改动分别属于「导入 / 设备声明 / 运行时接口 / 删除 GPU 逻辑 / kernel body」中的哪一类。
3. 重点确认：`@triton.jit def add_kernel(...)` 内部（约第 60–75 行）是否存在任何 `+`/`-` 标记。

**需要观察的现象**：所有带 `+`/`-` 的行都落在宿主侧（导入、device、断言），kernel body 部分没有任何增删标记。

**预期结果**：分类表大致如下——

| 改动 | 行（约） | 归类 |
| --- | --- | --- |
| `+import torch_npu` | 54 | 第 1 步 |
| `-DEVICE = ...get_active_torch_device()` | 58 | 第 4 步（删除） |
| `-assert x.device == DEVICE ...` | 79 | 第 4 步（删除） |
| `device='cuda' → device='npu'`（×2） | 87–90 | 第 2 步 |
| kernel body | 60–75 | **第 5 步：未改动** |

> 这是一项纯阅读实践，无需运行环境即可完成，用来巩固「迁移只动宿主侧」的心智模型。

#### 4.4.5 小练习与答案

**练习 1**：迁移后跑起来结果和 PyTorch 对不上，但 kernel body 你确实没动。最可能的失误在哪？

**参考答案**：按可能性排序排查——(a) 某处 `cuda` 设备声明漏改，导致输入张量其实不在 npu 上；(b) `import torch_npu` 漏写或位置太靠后；(c) 漏了 `torch.npu.synchronize()` 就提前读取结果；(d) 误删/误改了某个功能性同步。由于 kernel body 没动，问题几乎一定出在宿主侧的替换是否完整、干净。

**练习 2**：什么情况下才「允许」在迁移阶段修改 kernel body？

**参考答案**：只有当宿主侧替换已全部完成、正确性已验证、且确认是 kernel 本身的写法（而非设备迁移）导致问题时——例如触发了对齐、UB 溢出或 grid 超限（u2-l2/u2-l3 的话题）。在那之前，遵循第 5 步原则：先不动 kernel，优先验证正确性。

---

## 5. 综合实践

把本讲四个模块串成一个完整的端到端任务：**把一份 GPU 版 Triton 脚本迁移到 NPU 并跑通，产出一份替换 diff。**

**实践目标**：独立完成一次真实的「GPU → NPU」迁移，并验证结果正确。

**起始素材**：使用官方快速入门里给出的 GPU 版 `test_add.py`（[docs/en/quick_start.md:69-113](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/quick_start.md#L69-L113)）。把它另存为本地文件 `test_add.py`。

**操作步骤**：

1. 在 `import torch` 之后补上 `import torch_npu`。
2. 按 4.2 节把所有设备声明从 `cuda` 改成 `npu`：
   - `torch.cuda.current_device()` → `torch.npu.current_device()`；
   - `torch.device('cuda', device_id)` → `torch.device('npu', device_id)`；
   - `device='cuda'` → `device='npu'`（两处）；
   - `output_cpu.cuda()` → `output_cpu.npu()`。
3. 按 4.3 节把同步接口改掉：`torch.cuda.synchronize()` → `torch.npu.synchronize()`。
4. **不要**修改 `@triton.jit def add_kernel(...)` 内部任何一行（4.4 节第 5 步）。
5. 全文搜索 `cuda`，确认除注释外无残留。
6. 运行测试：
   ```bash
   source /usr/local/Ascend/ascend-toolkit/set_env.sh   # 按实际 CANN 路径
   pytest test_add.py
   ```
7. 用 `git diff`（或编辑器对比）导出你的替换 diff。

**需要观察的现象**：

- `pytest` 通过，`assert_close(output, output_torch, rtol=1e-3, atol=1e-3)` 不报错。
- 产出的 diff 里，`+`/`-` 全部集中在宿主侧，kernel body 无改动——形态应与 [docs/en/quick_start.md:128-159](https://github.com/triton-lang/triton-ascend/blob/0a5378d28abf6bbcd8d8916e03d397e9ed886a55/docs/en/quick_start.md#L128-L159) 官方 diff 一致。

**预期结果**：迁移成功，测试通过；diff 干净、可复现。如果 `pytest` 报错，回到 4.4.5 练习 1 的排查清单，**先怀疑宿主侧替换不完整，而不是 kernel**。

> 运行结果依赖真实 NPU 硬件与 CANN 环境，**待本地验证**。无硬件时，可把步骤 1–5、7（替换与 diff 产出）完整完成，把步骤 6 留到有设备时执行。

## 6. 本讲小结

- 迁移的**第一动作**是 `import torch_npu`——它是让 PyTorch 认识 `npu` 设备的适配层，没有它一切 `npu` 调用都不存在。
- **设备声明替换**几乎是「字面替换 `cuda → npu`」：`device='cuda'`、`.cuda()`、`.to('cuda')`、`torch.device('cuda', id)` 四种形态逐一改掉，但 kernel body 不动。
- **运行时接口替换**把 `torch.cuda.current_device()` / `synchronize()` / `set_device()` 映射到 `torch.npu.*`；而仅用于 GPU 设备发现的断言（如基于 `get_active_torch_device()` 的）应**删除**而非替换。
- Triton-Ascend **自身**探测设备用的就是 `torch.npu.current_device()`（见 backend_register.py），说明这套替换是项目代码里的真实调用，不是约定俗成。
- NPU 与 GPU 一样是**异步执行**模型，benchmark/校验时必须 `torch.npu.synchronize()` 后再读取结果。
- **第一原则**：先保持 kernel body 不变、只改宿主侧，优先验证正确性；性能、grid、对齐、UB 等深水区留给后续讲义。

## 7. 下一步学习建议

本讲只完成了「让它跑起来、结果对」这一步。当脚本变大、kernel 变复杂后，你会撞上 NPU 特有的硬件约束，这正是 u2 单元剩下两讲的主题：

- **u2-l2（NPU 硬件模型与 Grid 核心分配）**：理解 Cube Core 与 Vector Core 的分工，学会按算子类型（是否含 `tl.dot`）规划 grid 的并发任务数。当你发现「脚本能跑但很慢」或「grid 太大报 coreDim 超限」时，就需要它。
- **u2-l3（内存对齐、UB 与 coreDim 约束）**：深入 32B/512B 对齐、Unified Buffer 溢出、`coreDim ≤ UINT16_MAX` 这些迁移中真正会报错的话题，学会用 `BLOCK_SIZE`/tiling 控制它们。

如果你更想先理解「`device='npu'` 之后，Triton-Ascend 到底是怎么把它编译成二进制的」，可以先跳到 **u3（Triton 编译流水线总览）**，从 `@triton.jit` 一路追到 `AscendBackend` 的 `add_stages`，再回头处理迁移的性能问题。无论走哪条线，本讲建立的「宿主侧替换 + kernel body 不动 + 先验正确性」都是共同的前提。

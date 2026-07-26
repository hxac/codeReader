# 测试框架与基准框架

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 TileGym 测试体系的两条主线——**功能正确性测试**（`tests/ops`）与**性能基准**（`tests/benchmark`）。
- 看懂 `tests/common.py` 里的 `PyTestCase` 基类：`setUp`、`assertCorrectness`、`assertAllClose`、`compare_tensors`、`get_dtype_tolerances` 这些工具分别在做什么。
- 知道一篇 `tests/ops/test_*.py` 的标准骨架：`reference` 静态方法 + `@pytest.mark.parametrize` + `test_op` + `assertCorrectness`，以及如何用「后端参数化」让同一份断言覆盖 cuTile / tilecpp / triton 等多个后端。
- 理解 `tests/benchmark/bench_*.py` 用 `triton.testing.perf_report` 做基准的方式，以及 `run_all.sh` 如何自动发现并批量跑全部 `bench_*.py`。

本讲只讲**测试与基准框架本身**，不讲解某个具体内核的算法（那是 U3–U6 的事）。我们用 softmax 作为贯穿全文的样例，因为它最简单、且同时出现在测试和基准两侧。

## 2. 前置知识

本讲假设你已经读过 [u1-l3 第一次调用 TileGym 算子](u1-l3-first-op-call.md)，知道：

- `tilegym.ops.softmax(x, use_tma=False)` 是统一入口，真正计算由分发器在运行时按「当前后端」路由（参见 [u2-l2 后端注册表与分发机制](u2-l2-backend-dispatcher.md)）。
- `tilegym.set_backend("cutile")` 切换进程级后端，`tilegym.is_backend_available(backend)` 判断某后端在本机是否可用。

再补充几个本讲会用到的术语：

- **reference（参考实现）**：用一个「我们认为绝对正确」的版本（通常是 PyTorch 的官方算子）作为对照基准。测试的核心思想就是「我的 GPU 内核输出」与「参考实现输出」之差必须小于某个容差。
- **容差（tolerance）**：浮点数比较不可能要求「完全相等」。PyTorch 的 `torch.allclose` 用的是 `|a − b| ≤ atol + rtol·|b|`，其中 `atol`（absolute tolerance，绝对容差）允许一个固定误差，`rtol`（relative tolerance，相对容差）允许一个与 `|b|` 成比例的误差。低精度类型（fp16/bf16）的容差要大得多。
- **pytest 参数化（parametrize）**：用 `@pytest.mark.parametrize("参数名", [取值列表])` 把一个测试函数「展开」成多个独立的测试用例，每个用例对应一组参数。
- **fixture（夹具）**：pytest 的一种依赖注入机制，把「准备测试环境」的逻辑抽成一个函数，pytest 会在测试运行前后自动调用它。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tests/common.py` | 测试与基准的**公共工具库**。定义 `PyTestCase` 基类、`assertCorrectness`/`assertAllClose`/`compare_tensors` 比较工具、`get_dtype_tolerances` 容差表、`TestParam` 数据类，以及 `benchmark` / `benchmark_fn` / `benchmark_fn_cupti` / `benchmark_fn_cudagraph` 这一整套基准计时函数。 |
| `tests/config.py` | 全局配置 `Config`，用 `argparse` 解析命令行参数与同名环境变量（`WARMUP`/`REP`/`MODE`/`CUPTI`/`SEED` 等），供 `common.py` 读取。 |
| `tests/conftest.py` | pytest 配置。注册自定义 marker、定义 `arch` 等 fixture、注入 `--arch` 命令行选项。 |
| `tests/ops/test_softmax.py` | 功能正确性测试样例。展示 `Test_Softmax` 的标准骨架，是本讲的主样本。 |
| `tests/ops/README.md` | 测试框架的官方使用说明：命名规范、类结构、示例。 |
| `tests/benchmark/bench_softmax.py` | 性能基准样例。用 `triton.testing.perf_report` 做 softmax 的带宽基准。 |
| `tests/benchmark/bench_utils.py` | 基准辅助：`profile_with_l2flush`，用 `torch.profiler` 测「纯内核时间」并每次清 L2 缓存。 |
| `tests/benchmark/run_all.sh` | 批量跑全部 `bench_*.py` 的 shell 脚本，自动发现文件、保存结果、汇总失败项。 |

## 4. 核心概念与源码讲解

### 4.1 测试基类 PyTestCase 与容差体系

#### 4.1.1 概念说明

TileGym 的每一个功能测试类都继承自 `tests/common.py` 里的 `PyTestCase`。它不是 pytest 的内置类，而是 TileGym 自己写的「约定基类」：约定了**每个测试前后要做什么环境准备**、**怎么把我的内核和参考实现做比较**。

`PyTestCase` 自身不定义任何 `test_*` 方法，它只提供两类能力：

1. **测试环境管理**：每个测试开始前重置随机种子、回收 GPU 显存，保证测试之间互不干扰、可复现。
2. **正确性断言工具**：`assertCorrectness`、`assertAllClose`、`assertDeterministic` 等方法，封装了「跑 test 函数 → 跑 reference 函数 → 比较输出（必要时还比较梯度）」的完整流程。

它还配套了一个**容差表** `get_dtype_tolerances`：不同数据类型的浮点精度差异巨大，比较时必须用不同的 `rtol/atol`。fp64 容差是 `1e-12`，fp32 是 `1e-5`，fp16/bf16 是 `1e-2`，整数类型则是「完全相等」（容差为 0）。

#### 4.1.2 核心流程

一篇 `Test_Xxx` 测试在 pytest 里的生命周期：

```text
pytest 收集到 test_op
        │
        ▼
PyTestCase.setup_test（autouse fixture，每个测试都自动跑）
   把 request/node 名字挂到 self，供断言里打印用
        │
        ▼
test_op 内部第一行通常调用 self.setUp()
   ├─ gc.collect() + torch.cuda.empty_cache()   # 回收上一轮显存，降低 OOM
   └─ torch.manual_seed / random.seed            # 重置随机种子，保证可复现
        │
        ▼
构造输入张量 x、参考输出 dout
        │
        ▼
self.assertCorrectness(test_fn, ref_fn, kwargs, ...)
   ├─ 自动检测容差（按 kwargs 里第一个张量的 dtype 查表）
   ├─ 深拷贝张量 kwargs → 跑 test_fn → 跑 ref_fn
   ├─ compare_tensors 逐项比较输出（allclose）
   └─ 若输出 requires_grad，再反向传播比较输入梯度
        │
        ▼
通过 / assert 抛出失败信息
```

两个关键点先记住：**容差按 dtype 自动推断**（不传也能跑），**`setUp` 由测试自己在 `test_op` 里显式调用**（不是 pytest 自动触发，见 4.3 节）。

#### 4.1.3 源码精读

`PyTestCase` 的类定义与 docstring：

[tests/common.py:91-94](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L91-L94) —— 声明 `PyTestCase` 基类。它只是一个承载工具方法的容器。

每个测试自动执行的 fixture，把 pytest 的 `request` 存到 `self` 上：

[tests/common.py:96-102](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L96-L102) —— `setup_test` 用 `@pytest.fixture(autouse=True)` 标注，意味着**所有继承 `PyTestCase` 的测试都会自动运行它**。它把 `request.node.name`（即测试名）存到 `self.test_name`，后续打印匹配信息时能标明是哪个用例。

测试前的环境重置逻辑在 `setUp`：

[tests/common.py:104-117](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L104-L117) —— `gc.collect()` + `torch.cuda.empty_cache()` 主动释放上一轮测试残留的 GPU 显存（注释提到这是为了在显存吃紧的机器如 GB200 NVL 上降低 OOM 风险），随后把 `torch` 与 Python `random` 的随机种子重置为 `Config.seed`，保证每次输入数据一致、测试可复现。

容差表是这个体系的「度量衡」：

[tests/common.py:39-74](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L39-L74) —— `get_dtype_tolerances` 把每种 `torch.dtype` 映射到一组 `{rtol, atol}`。注意整数类型（int8/16/32/64、uint8、bool）容差为 0，即「逐位精确相等」；而 `float8_e5m2` 这种极低精度类型容差高达 `5e-1`。未知类型回退到 fp32 的默认值。

配套的「逐张量自定义容差」数据类：

[tests/common.py:663-671](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L663-L671) —— `TestParam` 把一个张量与它专属的 `rtol/atol` 打包在一起。当一个算子有多个输入、且各输入对精度的敏感度不同时（例如梯度比较），可以用 `TestParam(tensor, rtol=1e-3, atol=1e-3)` 给某个张量单独指定容差，覆盖全局默认。

#### 4.1.4 代码实践

**实践目标**：亲手感受「容差按 dtype 自动推断」的行为，不依赖任何 GPU 内核，纯 CPU 即可。

**操作步骤**：

1. 写一个临时脚本 `tmp_tol.py`（示例代码，放在仓库任意位置即可，不要放进 `tests/`）：

   ```python
   # 示例代码：演示 get_dtype_tolerances 的查表行为
   import torch
   from tests.common import get_dtype_tolerances

   for dt in [torch.float64, torch.float32, torch.float16,
              torch.bfloat16, torch.int32]:
       print(dt, get_dtype_tolerances(dt))
   ```

2. 运行 `python tmp_tol.py`（需在仓库根目录、且 `import torch` 可用）。

**需要观察的现象**：高精度类型容差极小、低精度类型容差大、整数类型为 0。

**预期结果**：

```text
torch.float64  {'rtol': 1e-12, 'atol': 1e-15}
torch.float32  {'rtol': 1e-05, 'atol': 1e-08}
torch.float16  {'rtol': 0.01,  'atol': 0.01}
torch.bfloat16 {'rtol': 0.01,  'atol': 0.02}
torch.int32    {'rtol': 0,     'atol': 0}
```

如果你的环境无法 `import tests.common`（例如没装项目），这一步可标注「待本地验证」，但容差的数值规律可从上面引用的源码直接读出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 fp16/bf16 的容差比 fp32 大好几个数量级？

> **参考答案**：fp16/bf16 的尾数位远少于 fp32，能表示的有效数字少，舍入误差天然更大；用 fp32 那种 `1e-5` 的容差去要求它们，正常计算都会被判失败，所以必须放宽到 `1e-2` 量级。

**练习 2**：`get_dtype_tolerances` 遇到表中没有的 dtype 会怎样？

> **参考答案**：调用 `dict.get(dtype, 默认值)`，对未知类型返回 fp32 的默认 `{rtol: 1e-5, atol: 1e-8}`，不会报错。

---

### 4.2 reference + assertCorrectness：正确性比较机制

#### 4.2.1 概念说明

`assertCorrectness` 是 `PyTestCase` 最核心的方法，它把「测试一个算子是否正确」浓缩成一次调用。你只需要提供四样东西：

- `test_fn`：被测函数（通常是 `tilegym.ops.某算子`）。
- `ref_fn`：参考实现（通常是一个 `@staticmethod reference`，用 PyTorch 官方算子实现）。
- `kwargs`：两个函数**共用**的关键字参数（输入张量等）。
- 可选的 `extra_test_kwargs` / `extra_ref_kwargs`：只有 test 或只有 ref 需要的额外参数（例如 cuTile softmax 独有的 `use_tma` 开关）。

它内部自动完成「自动选容差 → 深拷贝输入 → 跑 test → 跑 ref → 比较输出 →（若有梯度）比较输入梯度」。如果任一处不一致，会收集所有差异信息后一次性 `assert` 抛出，信息量很大（最大绝对误差、匹配百分比、不匹配的下标等），便于定位问题。

#### 4.2.2 核心流程

`assertCorrectness` 的比较分两阶段：

```text
[阶段一：前向输出比较]
  自动检测容差（扫 kwargs 找第一个张量，按其 dtype 查表）
  深拷贝 tensor kwargs  → ref_kwargs（防止就地内核污染输入）
  test_out  = test_fn(**fn_kwargs,  **extra_test_kwargs)
  empty_cache()                       # 释放 autotune 占用的显存
  ref_out   = ref_fn (**ref_kwargs,   **extra_ref_kwargs)
  对每个输出调用 compare_tensors(test_out, ref_out, rtol, atol)
     ├─ 形状/步长/dtype 三道校验
     ├─ torch.allclose 判定
     └─ 汇总：匹配百分比、最大绝对/相对误差、不匹配下标

[阶段二：梯度比较（仅当 test_out.requires_grad 且未开 multiple_outputs）]
  构造 gradient = ones_like(test_out)
  ref_out.backward(gradient)  → 收集 ref_grads
  test_out.backward(gradient) → 收集 test_grads
  对每个需要梯度的输入，用其专属容差（TestParam.rtol/atol）比较 ref_grads vs test_grads
```

容差判定的数学含义就是 `torch.allclose` 的定义。对张量里每个元素：

\[
|a_i - b_i| \;\le\; \text{atol} + \text{rtol}\cdot|b_i|
\]

其中 \(a_i\) 是被测输出、\(b_i\) 是参考输出。只有**所有元素**都满足上式，`allclose` 才为 `True`。注意这是一个「逐元素」条件，不是均值意义上的接近。

#### 4.2.3 源码精读

`assertCorrectness` 的完整签名与文档（参数含义都在这里）：

[tests/common.py:123-172](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L123-L172) —— 注意它支持的丰富选项：`gradient`（自定义反向梯度）、`multiple_outputs`（多输出）、`test_index/ref_index`（从多输出元组里挑一项比）、`output_processor`（对输出做预处理再比）、`check_stride`（顺带校验内存布局）。

容差自动检测逻辑：

[tests/common.py:184-217](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L184-L217) —— 当调用方没传 `rtol/atol` 时，它先扫 `kwargs` 再扫 `extra_test_kwargs`，找到第一个 `torch.Tensor`（或包在 `TestParam` 里的张量），取其 `dtype` 查 `get_dtype_tolerances`。找不到任何张量就回退到 fp32 默认值。这就是「不传容差也能跑」的原因。

深拷贝输入的精妙之处——防止就地（in-place）内核污染参考输入：

[tests/common.py:227-236](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L227-L236) —— 注释写得很清楚：rope_embedding、geglu/swiglu 的反向等内核会**就地改写输入张量**。如果不深拷贝，`test_fn` 跑完后输入已被改坏，再传给 `ref_fn` 就会比错。因此先 `ref_kwargs = {k: v.clone() ...}` 存一份干净的，再分别跑。中间还插了一句 `torch.cuda.empty_cache()`，释放 autotune 阶段预留的显存，防止后面大矩阵比较时 OOM。

前向输出的逐项比较：

[tests/common.py:251-264](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L251-L264) —— 把输出统一成列表（单输出也包成单元素列表），逐个调 `compare_tensors`。匹配就记一条 `MATCHED`，不匹配就记 `DID NOT MATCH` 并把详细差异信息塞进 `failed_msgs`。

梯度比较（可选的第二阶段）：

[tests/common.py:265-316](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L265-L316) —— 仅当 `test_out.requires_grad` 且未开 `multiple_outputs` 时进入。先对参考做 `backward` 收集梯度，清空 `.grad`，再对被测做 `backward`，逐输入用其专属容差比较。注意每个输入的梯度容差可以独立设置（通过 `TestParam.rtol/atol`），因为梯度往往比前向输出对精度更敏感。

底层比较器 `compare_tensors`，产出那份详尽的差异报告：

[tests/common.py:674-764](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L674-L764) —— 它先做形状/步长/dtype 三道硬校验（不一致直接 `RuntimeError`），再把两边都升到 fp32 做 `torch.allclose`，最后计算匹配百分比、最大绝对误差、最大相对误差、不匹配的下标列表。这份报告就是测试失败时你看到的那一大段信息，是排错的第一手资料。

#### 4.2.4 代码实践

**实践目标**：阅读 `test_softmax.py` 里那次 `assertCorrectness` 调用，逐参数对应到上面的源码逻辑。

**操作步骤**：

1. 打开 `tests/ops/test_softmax.py`，定位到 `test_op` 末尾的断言：

   [tests/ops/test_softmax.py:69-77](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L69-L77) —— 被测是 `tilegym.ops.softmax`，参考是类里的 `self.reference`（即 `torch.nn.functional.softmax(x, dim=-1)`）。共用的 `kwargs={"x": x}`；只有被测需要的 `extra_test_kwargs={"use_tma":..., "use_chunked":..., "use_multi_wave":...}`（因为 PyTorch 的 softmax 没有这些开关）。`gradient=dout` 提供了反向梯度。注意这里**显式传了 `rtol/atol`**，所以 4.2.3 里的自动检测分支不会触发。

2. 对照 `assertCorrectness` 源码回答：在这个调用里，阶段二（梯度比较）会不会执行？为什么？

**需要观察的现象 / 预期结果**：因为 `gradient=dout` 被显式传入，且 softmax 的 cuTile 当前实现只有前向（参见 [u3-l4 softmax 内核全解](u3-l4-softmax-deep-dive.md)），`test_out.requires_grad` 取决于输入 `x` 是否 `requires_grad`。而在 `test_softmax.py` 中 `x = torch.rand(...)` **没有**设置 `requires_grad=True`，所以 `test_out.requires_grad` 为 `False`，**阶段二不会执行**，只比前向输出。这是一个值得记住的细节：是否跑梯度比较，由「输出是否在计算图里」动态决定，而非硬编码。

> 说明：以上是对源码逻辑的静态分析，结论为「待本地验证」——你可以在 `test_op` 里临时加 `print(x.requires_grad, test_out.requires_grad)` 自行确认。

#### 4.2.5 小练习与答案

**练习 1**：如果我测试的算子会**就地修改输入**，`assertCorrectness` 还能正常工作吗？

> **参考答案**：能。它会在跑 `test_fn` 前对张量参数做 `clone()` 存进 `ref_kwargs`（[tests/common.py:231](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L231)），这样 `test_fn` 即使就地改了输入，`ref_fn` 拿到的仍是干净副本。

**练习 2**：`assertCorrectness` 失败时，为什么不是在第一个不一致的元素处立刻 `assert`，而是收集完所有信息再抛？

> **参考答案**：因为它把前向每个输出、每个输入的梯度都比完，把所有 `DID NOT MATCH` 的详细信息攒进 `failed_msgs`，最后一次性 `assert passed`。这样一次失败就能看到全部问题，而不是修一个跑一次、再发现下一个。

---

### 4.3 后端参数化测试：用一个测试覆盖多个后端

#### 4.3.1 概念说明

TileGym 的核心特性是「同一算子名、多个后端实现」（cuTile / tilecpp / triton / cutile-rs，详见 [u2-l2](u2-l2-backend-dispatcher.md) 与 U7）。测试自然要回答：**每个后端的实现都对吗？**

最朴素的做法是为每个后端写一个测试，但那样重复太多。TileGym 的做法是**后端参数化**：用 `@pytest.mark.parametrize("backend", [...])` 把后端名当成一个测试参数，让 pytest 把 `test_op` 展开成「cutile 版」「tilecpp 版」……每个版本内部用 `set_backend(backend)` 切到对应后端，再跑同一份 `assertCorrectness`。于是**一份断言覆盖所有后端**。

但有个现实问题：不是每台机器都装了所有后端（tilecpp 需要 nvcc≥13.3，cutile-rs 需要 cargo，参见 [u2-l3 后端选择与可用性](u2-l3-backend-selector.md)）。所以测试里要先 `is_backend_available(backend)` 探测，不可用就 `pytest.skip(...)` 跳过，而不是报失败。这让同一份测试在有/无某后端的机器上都能合理运行。

#### 4.3.2 核心流程

`Test_Softmax` 的参数化是一个**多层笛卡尔积**：

```text
@pytest.mark.parametrize("m,n,dtype", [7 组形状×精度])      # 第 1 维
@pytest.mark.parametrize("backend",        _backends)       # 第 2 维：后端
@pytest.mark.parametrize("use_tma,use_chunked,use_multi_wave", [4 组变体])  # 第 3 维
def test_op(self, m, n, dtype, arch, backend, use_tma, use_chunked, use_multi_wave):
    ...
```

pytest 会把这三层 `parametrize` 做笛卡尔积，于是 `test_op` 被展开成 `7 × |backends| × 4` 个独立用例。每个用例的执行流程：

```text
进入 test_op(某组参数)
   │
   ├─ if tilegym.is_backend_available(backend):
   │      tilegym.set_backend(backend)   # 切到该后端
   │      self.setUp()                    # 重置种子/显存
   │  else:
   │      pytest.skip(...)               # 本机没装 → 跳过，不计为失败
   │
   ├─ self.setUp()                        # 注意：又被调了一次（见下方说明）
   ├─ 构造 x = torch.rand(m, n, ...)
   ├─ 按 dtype 选 rtol/atol
   └─ self.assertCorrectness(tilegym.ops.softmax, self.reference, {"x": x}, ...)
```

`_backends` 列表本身也是**动态**的：默认只有 `["cutile"]`，只有当 `is_backend_available("tilecpp")` 为真时才追加 `"tilecpp"`。这样参数化的取值范围本身就随机器环境变化。

> 关于 `setUp` 被调用两次：这是 `test_softmax.py` 当前的一个写法细节——`if/else` 两个分支里各调了一次 `self.setUp()`。它不会出错（重置种子是幂等的），但严格来说调一次即可。理解时把它当成「确保进入断言前环境干净」即可。

#### 4.3.3 源码精读

`Test_Softmax` 的整体骨架：

[tests/ops/test_softmax.py:14-22](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L14-L22) —— 类名必须以 `Test_` 开头（pytest 收集规则 + 项目约定，见 README）。`reference` 是 `@staticmethod`，返回 `torch.nn.functional.softmax(x, dim=-1)`。`_backends` 在模块导入时按 `is_backend_available("tilecpp")` 动态决定要不要包含 tilecpp。`_perf_frameworks` 额外加了 `"pytorch"`，供基准测试用。

三层参数化装饰器：

[tests/ops/test_softmax.py:24-46](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L24-L46) —— 注意第三层用了 `ids=["baseline","use_tma","use_chunked","use_multi_wave"]`，给四组变体起人类可读的名字，pytest 输出时会显示这些 id 而非 `param0/param1`。形状里特意混了规整（2048）与非规整（9、1009）列宽，以及超大列宽（1024×32），用来压测边界处理与分块路径。

`test_op` 主体，含后端探测与跳过逻辑：

[tests/ops/test_softmax.py:47-52](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L47-L52) —— 这就是「后端参数化 + 优雅跳过」的关键。注意签名里的 `arch` 参数：它来自 `conftest.py` 的 `arch` fixture，本测试体内并未使用它，但它必须出现在签名里，pytest 才会注入该 fixture（否则会报参数不匹配）。`arch` 在别的测试里用于按 GPU 架构筛选用例。

`arch` fixture 的定义：

[tests/conftest.py:87-89](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/conftest.py#L87-L89) —— `arch` fixture 读取命令行选项 `--arch`。

`--arch` 命令行选项的注册（默认值即当前 GPU 的 compute capability）：

[tests/conftest.py:62-68](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/conftest.py#L62-L68) —— 默认值是 `sm{major}{minor}`，例如 `sm90`、`sm100`、`sm120`。你可以用 `pytest --arch=sm90` 覆盖，让按架构分流的测试按指定架构跑。

`tests/ops/README.md` 里写明的类结构约定：

[tests/ops/README.md:26-31](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/README.md#L26-L31) —— 官方约定：类名以 `Test_` 开头；继承 `common.PyTestCase`；实现 `reference` 方法；用 `@pytest.mark.parametrize` 做变体。这是新增测试时必须遵守的契约。

README 给出的标准示例模板：

[tests/ops/README.md:33-56](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/README.md#L33-L56) —— 这就是「最小可用测试」的样子：`reference` + `parametrize` + `test_op` + `assertCorrectness`。本讲的实践任务就是照这个模板写。

运行测试的标准命令：

[tests/ops/README.md:9-16](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/README.md#L9-L16) —— `pytest your_test_name -k test_op -v --log-cli-level=INFO`。`-k test_op` 只跑名为 `test_op` 的用例（排除其它 `test_*`），`-v` 显示每个参数化用例的展开结果。

#### 4.3.4 代码实践

**实践目标**：照 `test_softmax.py` 的结构，亲手写一个**最小的** `Test_Softmax` 骨架（精简版），并跑通它。这是本讲的主实践任务。

**操作步骤**：

1. 在 `tests/ops/` 下新建 `test_softmax_minimal.py`（**示例代码**，仅用于练习；练习结束可删除）：

   ```python
   # 示例代码：test_softmax 的最小骨架，用于练习测试框架
   # SPDX-License-Identifier: MIT
   import pytest
   import torch

   import tilegym
   from tilegym.backend import is_backend_available

   from .. import common


   class Test_SoftmaxMinimal(common.PyTestCase):
       # 1) 参考实现：用 PyTorch 官方算子当 ground truth
       @staticmethod
       def reference(x):
           return torch.nn.functional.softmax(x, dim=-1)

       # 2) 后端参数化：默认 cutile，可选 tilecpp
       _backends = ["cutile"]
       if is_backend_available("tilecpp"):
           _backends = _backends + ["tilecpp"]

       # 3) 形状/精度参数化（只留一组，保持最小）
       @pytest.mark.parametrize("m,n,dtype", [
           (256, 2048, torch.float32),
       ])
       # 4) 后端参数化
       @pytest.mark.parametrize("backend", _backends)
       def test_op(self, m, n, dtype, arch, backend):
           # 5) 后端探测 + 优雅跳过
           if tilegym.is_backend_available(backend):
               tilegym.set_backend(backend)
               self.setUp()
           else:
               pytest.skip(f"Backend {backend} is not available")

           device = torch.device("cuda")
           x = torch.rand(m, n, device=device, dtype=dtype)

           # 6) 正确性断言：一份断言覆盖所有后端
           self.assertCorrectness(
               tilegym.ops.softmax,
               self.reference,
               {"x": x},
               rtol=1e-5,
               atol=1e-7,
           )
   ```

2. 在仓库根目录运行：

   ```bash
   pytest tests/ops/test_softmax_minimal.py -k test_op -v
   ```

**需要观察的现象**：

- pytest 把 `test_op` 展开成 `|backends|` 个用例（本机若只有 cuTile，就是 1 个；若还有 tilecpp，就是 2 个）。
- 每个用例名里带 `backend_cutile` / `backend_tilecpp` 这样的参数标记。
- 不可用的后端显示为 `SKIPPED`（若你手动把 `_backends` 改成包含一个未安装的后端名），而非 `FAILED`。

**预期结果**：可用的后端全部 `PASSED`，不可用的后端 `SKIPPED`。如果本机没有 GPU 或没装好 cuTile，整个文件会因 import/探测失败——这种情况标注「待本地验证」，但你仍能从骨架中理解结构。这个骨架与 `test_softmax.py` 的差别仅在于：去掉了 `use_tma/use_chunked/use_multi_wave` 第三层参数化、把形状精简成一组、去掉了 `gradient`。把它扩展回多形状多变体，就是仓库里的 `test_softmax.py`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_backends` 要写成 `["cutile"]` 加上「条件追加 tilecpp」，而不是直接写死 `["cutile", "tilecpp"]`？

> **参考答案**：因为 tilecpp 不一定可用（需要 nvcc≥13.3，见 [u2-l3](u2-l3-backend-selector.md)）。若写死，在没有 tilecpp 的机器上，参数化会产生一个 `backend=tilecpp` 的用例，虽然 `test_op` 内部会 `pytest.skip`，但更干净的做法是从参数取值层面就排除它，减少无意义的用例展开。

**练习 2**：`test_op` 签名里的 `arch` 参数在 `Test_Softmax` 体内并未使用，去掉它会怎样？

> **参考答案**：会报错。`arch` 是 `conftest.py` 提供的 fixture，只有出现在 `test_op` 签名里，pytest 才会注入它。去掉签名里的 `arch`，pytest 会因「参数无来源」而拒绝收集该测试。它在这里「占位」是为了让 fixture 生效（其它测试会真正用 `arch` 按架构筛选用例）。

---

### 4.4 性能基准框架：bench_*.py 与 run_all.sh

#### 4.4.1 概念说明

功能测试回答「**对不对**」，性能基准回答「**快不快**」。TileGym 的基准代码在 `tests/benchmark/`，每个算子一个 `bench_*.py`。

基准框架分两层：

1. **计时引擎**（`tests/common.py` 里的 `benchmark` / `benchmark_fn` / `benchmark_fn_cudagraph` / `benchmark_fn_cupti`）：负责「热身 → 多次测量 → 统计」。它支持三种计时后端：
   - **CUDA Events**（`benchmark_fn`）：用 GPU 事件计时，包含 launch 开销。
   - **CUDA Graph**（`benchmark_fn_cudagraph`）：把多次调用录制成一张图重放，最小化 host 开销。
   - **CUPTI**（`benchmark_fn_cupti`，默认）：用 `torch.profiler` 走 CUPTI 回调，测**纯内核执行时间**，还能按内核名过滤。配置项 `Config.cupti` 默认就是 `True`。
2. **基准脚本**（`bench_*.py`）：用 `triton.testing.perf_report` + `triton.testing.Benchmark` 声明「扫哪个维度（x_names/x_vals）、比较哪些后端（line_vals）、报什么指标」，然后 `bench_xxx.run(print_data=True)` 跑起来。这套 `triton.testing` 工具来自 Triton，会自动画图、出表。

此外，`bench_*.py` 里普遍用一个更轻量的计时助手 `profile_with_l2flush`（在 `bench_utils.py`），它每次测量前清空 L2 缓存，避免数据驻留缓存导致测出「虚高」的带宽。

最后，`run_all.sh` 负责批量跑全部 `bench_*.py`：自动发现文件、把每个脚本的标准输出存成 `<文件名>_results.txt`、汇总失败项，并提供 `--json` 走另一套 JSON 输出。

#### 4.4.2 核心流程

一个 `bench_*.py` 的运行结构：

```text
模块加载
  ├─ 定义 reference_softmax，并用 register_impl("softmax", "torch")(reference_softmax)
  │    把「PyTorch 版」也注册成一个后端实现，便于在基准里和 cuTile 同台对比
  ├─ ALL_BACKENDS = [可选 cutile, 可选 tilecpp, 必有 torch]   # 按可用性过滤
  └─ create_benchmark_config(...) 返回一个 triton.testing.Benchmark
        （x_names=["N"], x_vals=[1024..16384], line_arg="backend",
         line_vals=[各后端名], ylabel="GB/s", ...）

@triton.testing.perf_report([各 config])
def bench_softmax(M, N, backend, dtype, ...):
    构造 x
    fn  = lambda: tilegym.ops.softmax(x, ..., backend=backend)   # 注意：调用级指定后端
    ref = lambda: reference_softmax(x)
    torch.testing.assert_close(fn(), ref(), ...)                 # 基准里也顺带验正确性
    ms = profile_with_l2flush(fn)                                # 测纯内核时间
    total_bytes = 2 * x.numel() * x.element_size()               # 读入 + 写出
    return total_bytes * 1e-9 / (ms * 1e-3)                      # 换算成 GB/s

bench_softmax.run(print_data=True)   # 触发：对每个 N × 每个 backend 跑一遍，出图出表
```

计时引擎 `benchmark_fn` 的内部节奏（以 CUDA Events 版为例）：

```text
1. 先用 initial_rep 次调用估算单次耗时 estimate_ms
2. 据预算（warmup ms / rep ms）算出 n_warmup、n_repeat
3. 分配 256MB 缓冲区 cache，每次测量前 cache.zero_() + cuda._sleep  → 清 L2
4. 跑 n_repeat 次，每次用一对 start/end Event 包住 fn()
5. 汇总：mean/std/rel_std/median/iqr/q25/q75/min/max/nrep/peak_mem_mb
   并附上「最耗时的内核名」与「各内核耗时清单」（来自一次 profiled run）
```

#### 4.4.3 源码精读

`bench_softmax.py` 把 PyTorch 实现注册成 `torch` 后端，方便同台对比：

[tests/benchmark/bench_softmax.py:16-26](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/bench_softmax.py#L16-L26) —— `register_impl("softmax", "torch")(reference_softmax)`。这正是 [u2-l2](u2-l2-backend-dispatcher.md) 讲的注册机制：算子名 `"softmax"` 是全局键、`"torch"` 是子键。注册后，`tilegym.ops.softmax(x, backend="torch")` 就会路由到这个 PyTorch 参考实现。于是基准脚本可以像调用其它后端一样调用 PyTorch，做横向对比。

按可用性过滤的后端清单：

[tests/benchmark/bench_softmax.py:30-34](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/bench_softmax.py#L30-L34) —— cutile 与 tilecpp 受 `is_backend_available` 门控（不可用则为 `None`，后面被 `get_supported_backends` 过滤掉），`torch` 恒在。每个后端配了显示名与绘图样式（颜色、线型）。

用 `triton.testing.Benchmark` 声明「扫什么、比什么」：

[tests/benchmark/bench_softmax.py:42-69](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/bench_softmax.py#L42-L69) —— `x_names=["N"]` 表示横轴是列数 N，`x_vals=[1024, 2048, ...]` 是取值；`line_arg="backend"` 表示「每条线对应一个后端」，`line_vals` 是各后端名、`line_names` 是图例名。`ylabel="GB/s"` 说明这个基准报的是**内存带宽**而非延迟。

把多种 (M, dtype, 变体) 组合各生成一个 Benchmark，再用 `perf_report` 装饰：

[tests/benchmark/bench_softmax.py:72-84](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/bench_softmax.py#L72-L84) —— 列表推导一次生成 `2 dtype × 4 变体` 共 8 个 Benchmark 配置，`@triton.testing.perf_report([...])` 把它们都挂到 `bench_softmax` 上。

基准函数本体：验正确性 + 测带宽：

[tests/benchmark/bench_softmax.py:85-110](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/bench_softmax.py#L85-L110) —— 关键三步：(1) 用 `backend=backend` **调用级指定后端**（不改变进程级后端，参见 [u2-l1](u2-l1-unified-op-interface.md) 里对显式 `backend=` 的讲解）；(2) `torch.testing.assert_close` 顺带验证正确性（基准里也防回归）；(3) `profile_with_l2flush(fn)` 测纯内核时间，再按「读入 + 写出 = 2 × numel × element_size」换算成 GB/s。

轻量计时助手，每次清 L2：

[tests/benchmark/bench_utils.py:10-31](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/bench_utils.py#L10-L31) —— `profile_with_l2flush` 用 `torch.profiler` 只统计 `device_time_total > 0` 的内核时间（纯 GPU 时间），且每次测量前分配一个 L2 大小的缓冲区并 `zero_()`，把缓存里的旧数据冲掉，确保每次测的是「从显存搬数据」的真实带宽。取所有轮次耗时的**中位数**，抗离群点。

计时引擎的总入口 `benchmark`（三选一调度）：

[tests/common.py:776-905](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L776-L905) —— 它先按 `use_cupti` / `use_cudagraph` 选 `_benchmark_fn`（默认 CUPTI），再按 `mode`（`forward`/`backward`/`auto`）决定测前向还是反向。注意 `kernel_filter` 只在 CUPTI 模式下有效（否则直接 `ValueError`）。`auto` 模式下，仅当输出 `requires_grad` 才会顺带测反向。

CUDA Events 计时引擎，含完整的统计字段：

[tests/common.py:1023-1121](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L1023-L1121) —— 这就是上面「核心流程」里描述的 5 步节奏。返回的字典字段（`mean/std/rel_std/median/iqr/q25/q75/min/max/nrep/peak_mem_mb`，外加可选的 `kernel_name`/`kernel_times`）是整个基准体系的统一产出格式。

配置类 `Config`：所有基准参数都从命令行/环境变量来：

[tests/config.py:51-55](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/config.py#L51-L55) —— `Config` 用元类 `CacheMeta` 把属性访问转发到 `Config.args`，所以 `Config.warmup` 实际读的是解析后的 `args.warmup`。

CUPTI 默认开启：

[tests/config.py:213-221](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/config.py#L213-L221) —— `--cupti` 默认 `True`。也就是说，默认情况下基准走 CUPTI（`torch.profiler`）而非 CUDA Events，目的是测「纯内核时间」并支持按内核名过滤。

批量执行脚本 `run_all.sh`：自动发现 `bench_*.py` 并保存结果：

[tests/benchmark/run_all.sh:71-96](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/run_all.sh#L71-L96) —— `find . -name 'bench_*.py' | sort` 递归发现全部基准脚本（排序保证顺序确定），对每个脚本：用相对路径派生输出文件名（把 `/` 替换成 `_`，例如 `suites/unsloth/bench_swiglu.py` → `suites_unsloth_bench_swiglu_results.txt`，避免命名冲突），`python3 "$file" | tee "$output_file"` 把输出同时显示并存盘，失败的进 `FAILED_BENCHMARKS` 数组最后汇总。

JSON 输出分支：

[tests/benchmark/run_all.sh:48-63](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/run_all.sh#L48-L63) —— 传 `--json` 时改走 `run_all_json.py`，产出结构化 JSON 而非文本表，便于后续程序化分析。

#### 4.4.4 代码实践

**实践目标**：在不跑 GPU 的前提下，读懂一个基准脚本「扫了什么、比了什么、报了什么指标」，并掌握 `run_all.sh` 的用法。

**操作步骤**：

1. 阅读 `bench_softmax.py` 的 `create_benchmark_config`（[L42-L69](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/bench_softmax.py#L42-L69)），回答：
   - 横轴 `x_names` / `x_vals` 是什么？
   - 每条线 `line_vals` 对应什么？
   - 报的指标 `ylabel` 是什么单位？
2. 在有 GPU 的环境里，进入基准目录跑单个基准：

   ```bash
   cd tests/benchmark
   python3 bench_softmax.py
   ```

3. 跑全部基准并保存结果：

   ```bash
   cd tests/benchmark
   ./run_all.sh ./my_results         # 文本结果存到 ./my_results/
   ./run_all.sh ./my_results --json  # 改用 JSON 输出
   ```

**需要观察的现象**：

- `bench_softmax.py` 会为每个 `(dtype, 变体)` 组合打印一张性能曲线（横轴 N、纵轴 GB/s、每条线一个后端），并附带 correctness 检查（`assert_close` 不通过会直接抛错）。
- `run_all.sh` 会逐个跑 `bench_*.py`，每跑完一个打印 `✓ PASSED` 或 `✗ FAILED`，最后汇总。

**预期结果**：

- 对问题 1：横轴是列数 `N`（取值 `2**10 .. 2**14`）；每条线是一个后端（cutile / tilecpp / torch，按可用性）；指标是内存带宽 `GB/s`（softmax 是带宽受限算子，故报带宽而非 TFLOPS）。
- 对问题 2、3：若本机无 GPU 或后端未装，脚本会报错或跳过——标注「待本地验证」。即便不能跑，你也应能从源码读出上面的结论。

#### 4.4.5 小练习与答案

**练习 1**：为什么 softmax 基准报的是「GB/s（带宽）」而不是「TFLOPS（算力）」？

> **参考答案**：softmax 对每个元素只做少量 exp/除法运算，却要读一遍、写一遍整个张量，是典型的**带宽受限（memory-bound）**算子。它的性能瓶颈在数据搬运而非计算，所以用带宽衡量更有意义；算力指标（TFLOPS）对它意义不大。`bench_softmax` 里 `total_bytes = 2 * x.numel() * x.element_size()` 正是按「读入 + 写出」算搬运量。

**练习 2**：`benchmark_fn` 每次测量前都要 `cache.zero_()` 并 `cuda._sleep(...)`，目的是什么？

> **参考答案**：清空 L2 缓存（用一个 256MB 的缓冲区覆写）并让 GPU「睡」一会儿，确保每次测量的输入数据不在缓存里、必须重新从显存搬运。否则连续多次调用同一算子时，数据会驻留在 L2 里，测出偏高（虚好）的带宽，失去代表性。`profile_with_l2flush`（[bench_utils.py:21-22](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/bench_utils.py#L21-L22)）做的是同样的事。

**练习 3**：`run_all.sh` 为什么用「相对路径把 `/` 换成 `_`」来命名输出文件？

> **参考答案**：因为基准脚本是递归发现的（`suites/`、`experimental/` 等子目录里都有 `bench_*.py`）。如果只用文件名（`bench_swiglu_results.txt`），不同子目录下同名脚本会互相覆盖；用相对路径（`suites_unsloth_bench_swiglu_results.txt`）能保证唯一，参见 [run_all.sh:71-76](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/benchmark/run_all.sh#L71-L76)。

## 5. 综合实践

把本讲四块知识串起来，完成一个「**为已有算子补一个最小测试 + 一个最小基准**」的小任务。我们继续用 softmax（保证可运行、可对照仓库已有版本）。

**任务**：

1. **测试侧**：在 `tests/ops/` 新建 `test_softmax_my.py`，写一个 `Test_SoftmaxMy(common.PyTestCase)`，要求：
   - `reference` 用 `torch.nn.functional.softmax(x, dim=-1)`。
   - 用两层 `parametrize`：一层 `(m,n,dtype)` 给两组（一组 fp32 规整列宽、一组 fp16 小列宽如 9），一层 `backend` 给 `_backends`（动态含 cutile、可选 tilecpp）。
   - `test_op` 里做后端探测 + `set_backend` + `setUp` + `assertCorrectness`，并按 dtype 给不同的 `rtol/atol`（参考 [test_softmax.py:64-67](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/ops/test_softmax.py#L64-L67)）。
   - 运行 `pytest tests/ops/test_softmax_my.py -k test_op -v`，确认全部 `PASSED`。

2. **基准侧**：阅读 `bench_softmax.py` 后，在 `tests/benchmark/` 新建 `bench_softmax_my.py`，把 `create_benchmark_config` 里的 `x_vals` 改成更小的范围（例如 `[2**i for i in range(10, 12)]`）、只保留 `baseline` 变体，运行 `python3 bench_softmax_my.py`，观察输出曲线是否符合「带宽随 N 增大趋于平稳」的直觉。

**验收标准**：

- 测试侧：两组形状 × 可用后端数的用例全部通过；手动把 `backend` 改成一个不存在的后端名时，该用例 `SKIPPED` 而非 `FAILED`。
- 基准侧：能说清「横轴是 N、纵轴是 GB/s、每条线是一个后端」；若本机无 GPU，则把上述命令与预期现象写下来，标注「待本地验证」。

> 提示：这两份「my」文件仅用于练习，完成后请删除，不要提交进仓库——真实新增算子的完整工作流（含接口 stub、注册、导出）在 [u9-l2 贡献新算子的完整工作流](u9-l2-add-new-op-workflow.md) 讲。

## 6. 本讲小结

- TileGym 测试体系分两条主线：`tests/ops`（功能正确性，pytest 驱动）与 `tests/benchmark`（性能基准，`triton.testing.perf_report` 驱动），两者共用 `tests/common.py` 的工具。
- `PyTestCase` 是测试基类：`setup_test` 是 autouse fixture 挂上下文，`setUp` 显式重置种子与显存；容差按 dtype 自动推断（`get_dtype_tolerances`），也可用 `TestParam` 逐张量覆盖。
- `assertCorrectness` 是核心断言：自动选容差 → 深拷贝输入防就地污染 → 跑 test 与 ref → `compare_tensors` 比前向输出 →（输出 `requires_grad` 时）再比输入梯度，失败时一次性抛出详尽差异报告。
- 后端参数化用 `@pytest.mark.parametrize("backend", _backends)` 让一份断言覆盖 cuTile/tilecpp/...，`_backends` 按可用性动态增减，不可用后端 `pytest.skip` 跳过。
- 基准计时引擎默认走 CUPTI（`torch.profiler`，测纯内核时间），`bench_*.py` 用 `triton.testing.Benchmark` 声明横纵轴与对比后端，`run_all.sh` 递归发现并批量执行、按相对路径命名结果文件。
- 新增测试的官方契约（README）：类名 `Test_` 开头、继承 `common.PyTestCase`、实现 `reference`、用 `parametrize`、用 `assertCorrectness`。

## 7. 下一步学习建议

- 若你要**真正新增一个算子**（含接口 stub、后端实现、注册、导出、测试、基准全链路），请接着学 [u9-l2 贡献新算子的完整工作流](u9-l2-add-new-op-workflow.md)，它把本讲的测试/基准步骤嵌进完整的贡献流程。
- 若你想深入**某个具体内核**（而非测试框架本身），按算子类别回到 U3–U6：逐元素/归一化（U3–U4）、GEMM（U5）、注意力（U6）。
- 若你对**基准计时的底层原理**（CUPTI vs CUDA Events vs CUDA Graph 的取舍、L2 清缓存、kernel_filter）感兴趣，可精读 `tests/common.py` 的 `benchmark_fn_cupti`（[L1155-L1353](https://github.com/NVIDIA/TileGym/blob/efbfefc760f608e4b04d32c36813a1291fe36f3c/tests/common.py#L1155-L1353)）与 [u8-l3 HF 推理基准与内核覆盖率](u8-l3-hf-bench-coverage.md) 里关于 nsys/kernel coverage 的部分。

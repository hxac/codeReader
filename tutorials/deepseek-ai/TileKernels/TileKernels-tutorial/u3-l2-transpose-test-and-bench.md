# 转置的测试与基准：参考实现、strided 输入、带宽

## 1. 本讲目标

上一讲（u3-l1）我们读完了批量转置 kernel 的「内部机关」：寄存器 4×4 翻转、共享内存 padding、swizzle 布局。但一个 kernel 写完只是开始——**怎么证明它算得对？怎么量化它跑得快？** 本讲就从 kernel 内部跳出来，看 TileKernels 如何用 pytest 把「正确性」和「性能」两件事都钉死。

学完本讲你应该能够：

- 复述 TileKernels 的「对拍范式」：生成测试数据 → 用 PyTorch 写参考实现 → 用 `assert_equal` 做位精确比对。
- 解释 `twice_stride` 为什么故意把输入变成非连续（strided）张量，以及它和 kernel 的 `T.StridedTensor` 契约如何对应。
- 用 `count_bytes` 统计读+写字节数、用 `benchmark_timer` 测延迟，并推导出 GB/s 带宽公式。
- 看懂 `benchmark_record` 写出的 JSONL 记录与回归检测机制。
- 区分「专用 torch 参考函数」与「测试里内联的 `x.T.contiguous()`」两种参考实现风格，并知道何时用哪种。

## 2. 前置知识

本讲默认你已经掌握：

- **u2-l1 / u3-l1**：TileLang 算子的 `@tilelang.jit` + `@T.prim_func` 骨架，以及转置 kernel 的六段式数据流。
- **pytest 基础**：`@pytest.mark.parametrize` 参数化、`@pytest.mark.benchmark` 自定义标记、fixture（测试夹具）的概念。
- **PyTorch 张量布局**：`shape`（每维大小）、`stride`（每维步长，即沿该维前进一个元素要跨过多少个底层元素）、`contiguous`（连续存储）的含义。
- **GPU 显存带宽**：HBM（高带宽显存）的峰值带宽是「带宽受限」算子的性能天花板，例如 H100 约 3.35 TB/s、B200 约 8 TB/s。

两个关键词先建立直觉：

- **对拍（differential testing）**：让「被测实现」和「可信参考实现」跑同一份输入，比较输出是否一致。这是验证 GPU kernel 正确性最实用的方法——你不必手工推算每个元素，只需相信 PyTorch 这套 CPU/GPU 参考不会错。
- **位精确（bitwise exact）**：两个张量的底层字节完全相同。转置只是搬数据、不做任何算术，因此它的输出必须和参考实现**逐位相同**，连最后一位都不能差。

## 3. 本讲源码地图

本讲围绕「测试与基准」这条线，涉及以下文件：

| 文件 | 作用 |
| --- | --- |
| `tests/transpose/test_transpose.py` | 转置的正确性测试与基准测试，是本讲主战场。 |
| `tile_kernels/testing/numeric.py` | 提供 `assert_equal`（位精确断言）、`count_bytes`（统计字节数）。 |
| `tile_kernels/testing/bench.py` | 提供 `dtype_to_str`、`make_param_id`、`make_param_key` 等基准工具。 |
| `tile_kernels/testing/generator.py` | 提供 `generate_num_tokens`、`generate_hidden_sizes` 等参数生成器。 |
| `tests/pytest_benchmark_plugin.py` | 提供 `benchmark_timer` / `benchmark_record` fixture、回归检测。 |
| `tile_kernels/torch/__init__.py` | torch 参考层的聚合入口，本讲用来讨论「专用参考 vs 内联参考」。 |
| `tile_kernels/transpose/batched_transpose_kernel.py` | 被测的 `transpose` / `batched_transpose` wrapper（u3-l1 已精读，本讲只看它的 strided 契约）。 |

## 4. 核心概念与源码讲解

### 4.1 对拍范式：生成数据 + torch 参考 + assert_equal

#### 4.1.1 概念说明

一个 GPU kernel 算得「对」与否，最稳的判断方式不是人脑推算，而是**对拍**：拿一个我们信得过的参考实现（通常是 PyTorch 自带的算子或一段简单的纯 PyTorch 代码），让它和被测 kernel 吃同一份输入，再比较两者的输出。

TileKernels 把这套对拍流程固化成了**三件套**，几乎所有正确性测试都遵循它：

1. **参数生成器** `generate_test_params_*`：用 `@pytest.mark.parametrize` 枚举出 `{num_tokens, hidden, dtype, ...}` 的多种组合，决定「测哪些形状」。
2. **数据生成器** `generate_test_data_*`：根据一组参数，在 GPU 上构造出具体的输入张量。
3. **对拍断言**：调用被测 kernel 得到 `y`，用 PyTorch 写出参考 `y_ref`，再用 `assert_equal(y, y_ref)` 判等。

转置的对拍还有一个**特殊性**：它是纯数据搬运，没有舍入误差，所以可以用「位精确」断言（而不是浮点近似断言）。这一点和后面量化模块（会有舍入）形成鲜明对比。

#### 4.1.2 核心流程

以 `test_batched_transpose` 为例，一次测试的执行流程是：

```text
pytest 枚举一组 params（来自 generate_test_params_batched_transpose）
        │
        ▼
generate_test_data_batched_transpose(params)  →  在 GPU 造输入 x
        │
        ▼
y     = tile_kernels.transpose.batched_transpose(x)   # 被测 kernel
y_ref = torch.transpose(x, 1, 2).contiguous()         # PyTorch 参考
        │
        ▼
assert_equal(y, y_ref)   # 逐位比较，不一致就抛断言失败
```

注意参数生成器里藏着一个细节：**正确性用例和基准用例走不同的参数集**。同一个生成函数接收 `is_benchmark` 参数，在「全量测试」(`TK_FULL_TEST=1`) 开启时，只对正确性用例（`is_benchmark=False`）额外加入边界值（如 `num_tokens=0`），基准用例不加——因为零规模场景测延迟没有意义。

#### 4.1.3 源码精读

先看参数生成器，它用三层 `for` 做笛卡尔积，决定要测哪些组合：

[tests/transpose/test_transpose.py:55-62](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L55-L62) 枚举 `(num_tokens, hidden, num_experts, dtype)` 的所有组合。`generate_num_tokens(64, ...)` 会把 token 数向上对齐到 64 的倍数（因为 kernel 要求 `shape_x % 64 == 0`），`generate_hidden_sizes()` 返回一组都是 64 倍数的 hidden 值。

数据生成器根据参数造输入张量：

[tests/transpose/test_transpose.py:35-43](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L35-L43) 先用 `torch.randn` 造 bf16 张量，若 `dtype` 是 `float8_e4m3fn` 再 `.to(...)` 转成 FP8。注意它总是先造 bf16 再转——因为 `randn` 不直接支持 FP8。

然后是正确性测试本体：

[tests/transpose/test_transpose.py:94-100](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L94-L100) 调被测 kernel、调 `torch.transpose(x,1,2).contiguous()` 作参考、用 `assert_equal` 判等。这里的参考就是**内联**写的，没有去 `tile_kernels/torch/` 里找专用函数（原因见 4.4）。

最关键的断言函数在 numeric 模块：

[tile_kernels/testing/numeric.py:5-23](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/numeric.py#L5-L23) 依次检查 dtype / shape / stride / device 四项元数据，最后用 `torch.equal(... .view(torch.uint8), ...)` 做**按底层字节的逐位比较**。

最后这一步是重点：`.view(torch.uint8)` 把张量重解释为无符号 8 位整数序列，`torch.equal` 再逐字节比对。这种写法对 FP8（`float8_e4m3fn`）尤其重要——FP8 的比较语义在 PyTorch 里并不直观，但比字节就毫无歧义。所以 `assert_equal` 适合「位精确」的算子（转置、reshape、拼接），而不适合有舍入误差的算子（量化、矩阵乘）——后者要用 `calc_diff`（见 u9-l1）。

> 补充：`assert_equal` 还检查 `stride` 相等。转置的输出和参考都是连续张量、形状相同，步长自然一致，所以能通过。

#### 4.1.4 代码实践

**实践目标**：亲手把「三件套」对拍范式跑通，理解每个环节。

**操作步骤**：

1. 只跑 batched_transpose 的正确性用例（不跑基准，避免需要 `--run-benchmark`）：
   ```bash
   pytest tests/transpose/test_transpose.py -k test_batched_transpose -v
   ```
2. 观察每个参数组合生成一个独立用例（`-v` 会把参数化的 test id 打印出来，形如 `test_batched_transpose[num_tokens=...-hidden=...-num_experts=...-dtype=...]`）。
3. 故意制造一次失败以观察 `assert_equal` 的报错信息：临时把参考改成错的（**仅作阅读理解，不要提交**），例如把 `torch.transpose(x, 1, 2)` 改成 `torch.transpose(x, 1, 2) * 1.001`（仅 bf16 用例可见效），重新跑一个用例，观察断言失败时打印的 `mask=...` 与不一致元素。

**需要观察的现象**：

- 第 1 步所有用例应全部通过（绿色）。
- 第 3 步断言失败时，`assert_equal` 会打印「哪些位置不一致」（`torch.nonzero(mask)`）以及两边的值，证明它是逐位比较而非整体近似。

**预期结果**：正确实现下全部通过；改动参考后能精确定位到不一致的元素。

> 如果本地没有 SM90/SM100 GPU 或未装好依赖，命令会失败，这种情况标记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `generate_test_data_batched_transpose` 要先造 bf16 再 `.to(float8_e4m3fn)`，而不是直接 `torch.randn(..., dtype=torch.float8_e4m3fn)`？

**答案**：`torch.randn` 只支持浮点类型（如 float32/bfloat16），不支持 FP8 dtype。所以必须先在 bf16 下采样，再转换。这也保证了 FP8 用例和 bf16 用例吃到的「原始随机分布」一致，便于对照。

**练习 2**：转置用 `assert_equal`（位精确），而后面量化算子用 `calc_diff`（浮点相似度）。请用一句话解释为什么不能互换。

**答案**：转置是纯数据搬运、无舍入，输出必须与参考逐位相同，故用位精确；量化涉及缩放与舍入，输出与高精度参考只能在浮点意义上接近，故用相似度。

---

### 4.2 twice_stride：故意构造非连续输入

#### 4.2.1 概念说明

如果你的 kernel 只测「连续张量」，那它在真实场景里很可能会翻车。原因是在 LLM 训练/推理管线里，很多张量是**别人算出来的 view**——它逻辑上是 `(M, N)`，但底层存储可能是某个更大张量的一片，行与行之间隔着「空隙」。这种张量叫**非连续（non-contiguous）/ strided** 张量：它的 `stride` 不等于「按 shape 推出来的连续步长」。

`twice_stride` 就是用来**故意制造**这种带空隙的输入的辅助函数，目的是验证转置 kernel 能正确读懂 strided 布局，而不是偷偷假设「输入一定连续」。

#### 4.2.2 核心流程

`twice_stride(w)` 把一个 `(M, N)` 的连续张量改造成「行步长翻倍」的非连续张量：

```text
原始 w:  shape=(M, N),  stride=(N, 1)        ← 连续
        │
        ▼  new_empty((M, 2N))  → 申请一个 2N 宽的物理缓冲
        ▼  chunk(dim=1)[0]     → 取左半片，逻辑形状仍是 (M, N)
        ▼  ret[:] = w          → 把数据拷进左半片
结果 ret: shape=(M, N),  stride=(2N, 1)       ← 非连续！行步长被撑成 2N
```

关键在于：逻辑上 `ret` 的形状仍是 `(M, N)`，但它的行步长是 `2N` 而不是 `N`——也就是说，相邻两行在内存里隔了 `2N` 个元素（中间留了 N 个元素的「空隙」）。`ret.is_contiguous()` 会返回 `False`，这正是我们要的。

这和 kernel 的契约直接对应。被测 wrapper 在启动前会断言输入满足一定布局：

[tile_kernels/transpose/batched_transpose_kernel.py:107](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L107) 要求 `x.stride(-2) % 4 == 0 and x.stride(-1) == 1`——即最后一维必须连续（步长 1），倒数第二维步长只需 4 的倍数即可，**不要求整体连续**。`twice_stride` 造出的 `stride(-2)=2N`（N 是 64 的倍数，故 2N 是 4 的倍数）正好满足这个宽松契约。

对应地，kernel 用 `T.StridedTensor` 接收一个运行时符号 `stride_x` 来描述这个行步长：

[tile_kernels/transpose/batched_transpose_kernel.py:40](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/transpose/batched_transpose_kernel.py#L40) 形参 `x` 的类型是 `T.StridedTensor[(num_batches, shape_x, shape_y), (shape_x*stride_x, stride_x, 1), dtype]`，第二组元组就是三维的物理 stride，其中 `stride_x` 由启动时的张量提供，可以是 `2N` 这种「带空隙」的值。

#### 4.2.3 源码精读

`twice_stride` 的实现只有 6 行，但每一行都有用意：

[tests/transpose/test_transpose.py:14-20](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L14-L20) 注释直说「Make a 2D tensor's leading dim twice strided」。`new_empty` 分配未初始化的物理缓冲（速度快），`chunk(...)[0]` 取左半片得到一个 strided view，最后 `ret[:] = w` 把真实数据写进左半片，右半片保持未初始化（无所谓，因为 kernel 不会读那里）。结尾 `assert not ret.is_contiguous()` 是一道自检，确保确实造成了非连续。

然后在数据生成器里，**仅当 `num_tokens > 0`** 才施加 `twice_stride`：

[tests/transpose/test_transpose.py:23-32](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L23-L32) 注意 `if num_tokens > 0: x = twice_stride(x)`。零规模场景（`num_tokens==0`）只测「不崩溃」，不测数值，所以不需要构造 strided 输入。

> 对比：`generate_test_data_batched_transpose`（[L35-43](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L35-L43)）**没有**调用 `twice_stride`。也就是说，2D 的 `transpose` 测了 strided 输入，3D 的 `batched_transpose` 只测连续输入。这是一个覆盖面上的差异，值得在练习里思考。

#### 4.2.4 代码实践

**实践目标**：直观看到 `twice_stride` 改变了 stride 而非 shape，并验证 kernel 仍能正确转置。

**操作步骤**：

1. 在本地 Python（需 GPU + 已安装项目）里跑下面这段「示例代码」（**非项目原有代码**）：
   ```python
   # 示例代码：观察 twice_stride 对 stride 的影响
   import torch
   w = torch.randn((4032, 576), dtype=torch.bfloat16, device='cuda')
   print("contiguous:", w.is_contiguous(), w.stride())      # True  (576, 1)
   twice = w.new_empty((w.shape[0], w.shape[1] * 2))
   ret = torch.chunk(twice, 2, dim=1)[0]
   ret[:] = w
   print("contiguous:", ret.is_contiguous(), ret.stride(), ret.shape)  # False (1152, 1) [4032,576]
   ```
2. 把 `ret` 喂给 `tile_kernels.transpose.transpose`，再用 `ret.T.contiguous()` 作参考，手动调用 `assert_equal` 对拍。
3. 思考：如果把 `twice_stride` 里的 `* 2` 改成 `* 3`（行步长变成 3N，仍是 4 的倍数），kernel 还能跑吗？为什么？

**需要观察的现象**：

- 第 1 步：`ret` 的 shape 仍是 `(4032, 576)`，但 stride 变成 `(1152, 1)`（= `(2*576, 1)`），`is_contiguous()` 为 `False`。
- 第 2 步：kernel 输出与参考**逐位相同**，证明 kernel 正确读取了 strided 输入。

**预期结果**：strided 输入下数值依然正确，验证了 `T.StridedTensor` 契约生效。

> 实际运行结果「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`twice_stride` 为什么用 `new_empty` + `chunk` + 拷贝，而不是直接 `torch.as_strided`？

**答案**：`new_empty` 先分配一块物理上「2N 宽」的连续缓冲，`chunk` 取其左半片得到一个 stride 为 `(2N,1)` 的 view，再写入真实数据。这样既保证了「物理上有空隙」（非连续），又保证了「逻辑数据正确」。直接 `as_strided` 虽然也能改 stride，但指向的内存可能未初始化或越界，不如这种方式安全直观。

**练习 2**：2D `transpose` 测了 strided 输入，3D `batched_transpose` 没测。这是 bug 还是设计？请给出你的判断与理由。

**答案**：更可能是覆盖不完整而非有意设计。`batched_transpose` 的 wrapper 同样接受 `T.StridedTensor`（支持任意 `stride_x`），逻辑上同样需要 strided 测试。这恰好是读者可以补充测试的一个切入点（见综合实践）。

---

### 4.3 基准测试与带宽度量：count_bytes + benchmark_timer + benchmark_record

#### 4.3.1 概念说明

「跑得对」之后是「跑得快」。但「快」是个模糊词——同一个 kernel，在大形状下跑 100μs，在小形状下跑 5μs，谁更快？没法直接比。对于**带宽受限**的算子（转置几乎不计算、只搬数据），正确的性能指标不是「延迟」，而是**有效带宽（effective bandwidth）**：单位时间里搬了多少字节。

TileKernels 的基准测试做三件事：

1. **`count_bytes`**：统计这次 kernel 调用一共「读入 + 写出」了多少字节。
2. **`benchmark_timer`**：用 CUPTI（GPU 专用计时后端）多次重复测量，返回中位延迟（微秒）。
3. **`benchmark_record`**：把 `kernel/operation/params/time_us/bandwidth_gbs` 写成一条 JSONL 记录，并与历史 baseline 比对做回归检测。

带宽就把前两者合起来：

\[ \text{bandwidth (GB/s)} = \frac{\text{num\_bytes (B)}}{\text{t\_us (\mu s)}} \times 10^{-3} \]

单位推导：`num_bytes / t_us` 的量纲是「字节每微秒」。因为 \(1\,\mu s = 10^{-6}\,s\)、\(1\,\text{GB} = 10^{9}\,\text{B}\)，所以 \(1\,\text{B/\mu s} = 10^{6}\,\text{B/s} = 10^{-3}\,\text{GB/s}\)。于是「字节每微秒」乘以 \(10^{-3}\) 就得到 GB/s，这正是代码里 `num_bytes / t_us / 1e3` 的由来。

#### 4.3.2 核心流程

一个基准用例的执行流程：

```text
@pytest.mark.benchmark 标记 → 默认跳过，需 --run-benchmark 才收集
        │
        ▼
generate_test_data_*(params)                # 造输入
num_bytes = count_bytes(x, batched_transpose(x))   # 统计 读+写 字节
t_us      = benchmark_timer(lambda: batched_transpose(x))  # CUPTI 计时(μs)
        │
        ▼
benchmark_record(kernel='batched_transpose', operation='fwd',
                 params={**params, 'dtype': dtype_to_str(...)},
                 time_us=t_us,
                 bandwidth_gbs=num_bytes / t_us / 1e3)   # 写 JSONL + 回归检测
```

注意 `count_bytes(x, batched_transpose(x))` 里 `batched_transpose(x)` 会真实跑一次以产生输出张量，这样 `count_bytes` 才能量到「输入 + 输出」两边的字节数。对于 `(B,M,N) → (B,N,M)` 的转置，输入输出元素数相同，所以 `num_bytes = 2 × B × M × N × element_size`（读一份 + 写一份）。

`benchmark_timer` 内部调用 `tilelang.profiler.bench.do_bench`，默认 `rep=30`（重复 30 次取统计量）、`backend='cupti'`（用 NVIDIA CUPTI 而非 CPU 计时，避免 host-device 异步误差），返回值是毫秒，乘以 \(10^3\) 转成微秒。

#### 4.3.3 源码精读

`count_bytes` 递归地把每个张量的 `numel() * element_size()` 加起来：

[tile_kernels/testing/numeric.py:58-65](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/numeric.py#L58-L65) 支持传入多个张量（或嵌套的 tuple/list），跳过 `None`。它统计的是「张量逻辑大小」，对 strided 张量也是按逻辑 `numel()` 算，不包含物理空隙——这与「有效带宽」的定义一致。

`benchmark_timer` fixture：

[tests/pytest_benchmark_plugin.py:428-447](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L428-L447) 默认 `backend='cupti', warmup=0, rep=30`，允许调用方用关键字参数覆盖（如 `benchmark_timer(fn, rep=100)`）。返回值 `* 1e3` 把毫秒换成微秒。

`benchmark_record` fixture：

[tests/pytest_benchmark_plugin.py:358-425](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/pytest_benchmark_plugin.py#L358-L425) 做四件事：① 用 `make_param_key(params)` 拼出稳定 key（形如 `batched_transpose/fwd[...]`）；② 打印一行人类可读摘要 `BENCH ...: X.X us, bandwidth_gbs=...`；③ 若指定了 `--benchmark-output`，追加写一行 JSONL；④ 把记录收集进 `config._benchmark_results`，供会话末尾的回归报告使用。

JSONL 的 schema（文档化在 fixture docstring 里）：

```json
{
  "kernel": "batched_transpose",
  "operation": "fwd",
  "params": {"num_tokens": 4032, "hidden": 2048, "num_experts": 8, "dtype": "bf16"},
  "time_us": 12.34,
  "bandwidth_gbs": 2700.5
}
```

其中 `dtype` 必须先经过 `dtype_to_str` 转成字符串：

[tile_kernels/testing/bench.py:59-70](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/testing/bench.py#L59-L70) 把 `torch.float32/bfloat16/float8_e4m3fn/int8` 映射成 `fp32/bf16/e4m3/e2m1`。把 dtype 写成字符串是为了让 baseline key 跨版本稳定可比（torch 的 dtype 对象本身不好序列化进 key）。

转置基准用例本体：

[tests/transpose/test_transpose.py:103-117](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L103-L117) 是上述三步的具体落地。注意 `params={**params, 'dtype': dtype_to_str(params['dtype'])}`——把 dtype 替换成字符串后再传给 `benchmark_record`，保证 key 与 JSONL 里都是可读字符串。

会话末尾，插件会把每条记录与 `tests/benchmark_baselines.jsonl` 里的 baseline 比对：若 `current_us / baseline_us > 1 + 阈值`（默认阈值 0.15，即慢 15%）就判为回归，并让 pytest 退出码非 0（详见 u9-l2）。

#### 4.3.4 代码实践

**实践目标**：跑通一个基准用例，亲手算出 GB/s 带宽，并与硬件显存带宽天花板对比。

**操作步骤**：

1. 跑 batched_transpose 的基准（需要 `--run-benchmark`），并把结果写进 JSONL：
   ```bash
   pytest tests/transpose/test_transpose.py::test_batched_transpose_benchmark \
       --run-benchmark --benchmark-output=/tmp/tk_bench.jsonl -v
   ```
2. 观察终端每条 `BENCH ...: X.X us, bandwidth_gbs=...` 行；再 `cat /tmp/tk_bench.jsonl` 看 JSONL 记录。
3. 任选一条记录，用公式手算验证：`bandwidth_gbs ≈ num_bytes / t_us / 1e3`。其中 `num_bytes = 2 × num_experts × num_tokens × hidden × element_size`（bf16/FP8 是 2 字节，fp32 是 4 字节）。
4. 查你的 GPU 的 HBM 峰值带宽（如 H100 SXM 约 3350 GB/s、B200 约 8000 GB/s），计算 `实测带宽 / 峰值带宽` 这个利用率。

**需要观察的现象**：

- 大形状（hidden=2048、num_tokens 较大）下，实测带宽应较高（更接近峰值）；小形状下带宽偏低（launch 开销占比大）。
- bf16 与 FP8 的字节数相同（都 2 字节），若延迟接近，则带宽也接近。

**预期结果**：大形状下转置的有效带宽应能达到峰值带宽的一个较高比例（经验上带宽受限算子能到峰值的较高百分比）；若实测带宽远低于峰值，说明 kernel 或 launch 还有优化空间。

> 具体数值「待本地验证」，取决于你的 GPU 型号与形状组合。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `count_bytes` 要把「输入 + 输出」都算进去，而不是只算输入？

**答案**：转置是带宽受限算子，它的时间几乎全花在「从 HBM 读输入」和「向 HBM 写输出」上。有效带宽要反映「总共搬了多少数据」，所以必须同时计入读和写。只算输入会低估一半，让 kernel 看起来比实际更「省带宽」。

**练习 2**：基准 key 用 `make_param_key` 生成、dtype 用 `dtype_to_str` 转字符串。为什么 baseline 比对要追求 key 的「稳定可比」？

**答案**：回归检测要把「今天的延迟」与「历史 baseline」按 key 一一对应比较。如果 key 不稳定（比如某天 dtype 写成对象、另一天写成字符串），同一条用例会被当成两条，无法比对，回归检测就失效了。字符串化 + 排序是为了让 key 跨时间、跨机器都一致。

---

### 4.4 torch 参考层 tile_kernels/torch/__init__.py：专用参考 vs 内联参考

#### 4.4.1 概念说明

`tile_kernels/torch/` 是项目的「torch 参考层」：用纯 PyTorch（不写 TileLang、不写 CUDA）实现的算子参考版本，专门给测试对拍用。它存在的意义是——当被测 kernel 的数学逻辑非平凡时，参考实现也会很长，单独写成一个文件、对外导出，比内联在测试里更清晰、更可复用。

但**不是每个算子都需要专用参考**。像转置这种「PyTorch 一行就能写」的算子，参考实现就是 `x.T.contiguous()` 或 `torch.transpose(x,1,2).contiguous()`，没必要再封装一个函数。所以转置测试选择**内联**写参考，而 `tile_kernels/torch/__init__.py` 里并没有 `transpose_ref`。

这一讲我们读 `tile_kernels/torch/__init__.py`，目的不是记它导出了哪些函数，而是理解**「专用参考 vs 内联参考」的取舍标准**：参考实现的复杂度是否高到值得单独维护。

#### 4.4.2 核心流程

判断一个算子该用哪种参考，决策流程是：

```text
算子的参考实现有多复杂？
        │
        ├── 一行 PyTorch（如 x.T.contiguous()）
        │       → 内联写在测试里，不进 torch/ 目录
        │         （转置、reshape 属于此类）
        │
        └── 多行、含循环/分块/特殊数值处理（如量化、topk、sinkhorn）
                → 单独写进 tile_kernels/torch/<name>.py
                → 在 tile_kernels/torch/__init__.py 聚合导出
                → 测试里 import 后调用
```

#### 4.4.3 源码精读

聚合入口列出了所有「值得单独维护」的参考函数：

[tile_kernels/torch/__init__.py:1-11](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tile_kernels/torch/__init__.py#L1-L11) 导出了量化（`cast`/`cast_back`/`cast_to_e5m6`）、MoE（`stable_topk`/`top2_sum_gate`/`expand_to_fused`/`reduce_fused`/`group_count`/...）、MHC（`sinkhorn_normalize_ref`/`mhc_pre_*`）、SwiGLU 等参考。

注意几个细节：

- **没有转置参考**：列表里找不到 `transpose_ref`，印证了转置测试用内联参考的设计。
- **命名带 `_ref` 后缀**：如 `mhc_post_ref`、`sinkhorn_normalize_ref`、`expand_to_mhc_ref`，明确标注「这是参考实现」，避免和被测实现混淆。
- **FP8/E5M6 等特殊格式**：像 `cast_to_e5m6`、`cast_back_from_e5m6` 这种参考实现涉及大量位级特殊值处理（见 u4），写起来很长，必须单独成文件。

对比之下，转置测试里的参考就一句话：

[tests/transpose/test_transpose.py:98](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L98) `y_ref = torch.transpose(x, 1, 2).contiguous()`——这么短，内联显然比单开一个参考文件更合理。

#### 4.4.4 代码实践

**实践目标**：通过阅读 `tile_kernels/torch/` 的导出清单，归纳出「何时需要专用参考」的判断标准。

**操作步骤**：

1. 打开 `tile_kernels/torch/__init__.py`，把导出的参考函数分成三类：量化类、MoE 类、MHC 类。
2. 任选其中一个（例如 `tile_kernels/torch/topk.py` 里的 `stable_topk`），粗读它的实现长度与复杂度（是否含循环、并列处理、掩码等）。
3. 回到 `tests/transpose/test_transpose.py`，对比转置参考的长度（一行）。
4. 写下你的判断标准：参考实现超过多少行、含哪些特征时，就值得单独放进 `tile_kernels/torch/`。

**需要观察的现象**：

- 专用参考函数普遍较长、含控制流或特殊值处理；转置参考只有一行纯表达式。

**预期结果**：归纳出类似「参考实现若含循环/分块/掩码/位操作，或会被多个测试复用，就应单独成文件；否则内联」的判据。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `tile_kernels/torch/` 里的 MHC 参考都带 `_ref` 后缀，而 `cast`/`stable_topk` 不带？

**答案**：`_ref` 后缀用于强调「这是对照用参考、不是高性能实现」，在 MHC 这种参考与被测实现差异较大、容易混淆的场景下更醒目。`cast`/`stable_topk` 不带后缀，可能是因为它们本身就是该算子的「标准语义定义」，名字本身已足够清晰。这是一种命名风格上的权衡，并非强制规则。

**练习 2**：如果未来转置测试要在多处复用同一个「带特殊 padding 的转置参考」，应该怎么做？

**答案**：届时就该把参考从测试里抽出来，写成 `tile_kernels/torch/transpose.py` 里的 `transpose_ref`，并在 `tile_kernels/torch/__init__.py` 导出。即「复用需求」会把一个内联参考推升为专用参考。

---

## 5. 综合实践

**任务**：为 `batched_transpose` 新增一组 benchmark 参数（覆盖不同 dtype / shape / num_experts），运行并报告 GB/s 带宽，判断其是否接近硬件显存带宽极限；顺带补一个 strided 输入的正确性用例。

**操作步骤**：

1. **扩展参数集**：在 `tests/transpose/test_transpose.py` 里，仿照 [L55-62](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L55-L62) 的写法，新增一个 `generate_test_params_batched_transpose_extra`（或直接在原函数里加一个更大的 `num_experts`、一组更大的 `hidden`）。注意保持 hidden 是 64 的倍数、num_tokens 经 `generate_num_tokens(64, ...)` 对齐。
2. **补 strided 用例**：参考 2D 版的 `twice_stride` 调用（[L30-31](https://github.com/deepseek-ai/TileKernels/blob/36d9e45d38e204ebb87e6f6e833821eee0482fe5/tests/transpose/test_transpose.py#L30-L31)），给 `generate_test_data_batched_transpose` 也加上 strided 构造（对 `num_tokens` 维做 twice_stride），补上 4.2 里发现的覆盖缺口。先确认 wrapper 的 stride 契约（`stride(-2) % 4 == 0`）仍满足。
3. **跑基准**：
   ```bash
   pytest tests/transpose/test_transpose.py::test_batched_transpose_benchmark \
       --run-benchmark --benchmark-output=/tmp/tk_bench.jsonl -v
   ```
4. **算利用率**：从 JSONL 取出每条 `bandwidth_gbs`，除以你 GPU 的 HBM 峰值带宽（如 H100≈3350 GB/s、B200≈8000 GB/s），得到利用率。整理一张表：`(dtype, hidden, num_experts, num_tokens) → 延迟 μs / 带宽 GB/s / 利用率`。
5. **分析**：找出利用率最高和最低的两条，解释原因（形状大小、launch 开销、是否触及带宽极限）。

**需要观察的现象与预期结果**：

- 大形状、bf16/fp32 的组合利用率应较高；极小形状利用率偏低。
- 若最高利用率已接近峰值（经验上较高的比例），说明该 kernel 已是「带宽受限且优化良好」；若普遍偏低，可结合 u3-l1 的 swizzle/padding 知识猜测瓶颈。

> 具体带宽数值「待本地验证」，取决于硬件。本任务只要求流程正确与分析自洽，不要求达到某个绝对数值。

## 6. 本讲小结

- TileKernels 的正确性测试遵循**三件套对拍范式**：`generate_test_params_*` 枚举形状 → `generate_test_data_*` 造输入 → 被测 kernel 与 PyTorch 参考用 `assert_equal` 做**位精确**判等。
- 转置是纯数据搬运，故用位精确断言（`assert_equal` 按 `uint8` 字节比较）；有舍入的算子（量化）改用浮点相似度（`calc_diff`）。
- `twice_stride` **故意制造非连续输入**（行步长翻倍），配合 kernel 的 `T.StridedTensor` + `stride_x` 运行时符号，验证 kernel 不依赖「输入连续」的隐藏假设。
- 基准测试用 **有效带宽** 衡量性能：`bandwidth_gbs = num_bytes / t_us / 1e3`，其中 `count_bytes` 统计读+写、`benchmark_timer` 用 CUPTI 测延迟（μs）。
- `benchmark_record` 把结果写成稳定 key 的 JSONL 记录，并与 `benchmark_baselines.jsonl` 比对做 15% 阈值的**回归检测**。
- `tile_kernels/torch/` 只收纳**复杂、可复用**的专用参考；像转置这种一行就能写的参考，直接内联在测试里，所以 `torch/__init__.py` 里没有 `transpose_ref`。

## 7. 下一步学习建议

- **横向对比另一个算子的测试**：阅读 `tests/quant/` 或 `tests/moe/` 下的某个测试，体会 `assert_equal`（位精确）与 `calc_diff`（浮点）在不同算子里的选用差异，这也是 u9-l1 的主题。
- **深入测试基础设施**：本讲用到的 `assert_equal` / `count_bytes` / `check_bias` 的统计原理、`generator` 与 `TK_FULL_TEST` 的关系，将在 **u9-l1（测试数据生成与数值对拍）** 系统讲解。
- **深入基准设施**：`benchmark_timer` 的 CUPTI 后端、`benchmark_baselines.jsonl` 的回归报告、xdist 多卡内存切分，将在 **u9-l2（benchmark 插件与回归检测）** 展开。
- **回到 kernel 内部**：若你想搞清楚转置为什么能达到高带宽利用率，可重温 **u3-l1** 的寄存器转置、padding 与 swizzle 布局。
- **走向 modeling 层**：当你想把这些底层 kernel 包成可求导的 PyTorch 层，请进入 **u8-l1（torch.autograd.Function 封装范式）**。

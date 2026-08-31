# 算子的两种变体：allocating 与 _into

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `cvcuda.flip(src, ...)` 与 `cvcuda.flip_into(dst, src, ...)` 在**语义**与**内部执行路径**上的确切差异。
2. 沿着源码走一遍 allocating 变体多出来的那条路：`Tensor::Create` → `Cache::fetch` → 命中复用或新建入缓存。
3. 理解变长批（`ImageBatchVarShape`）的 allocating 变体为什么比固定张量批"重得多"。
4. 根据自己的管线形态（固定形状循环 / 形状多变原型）做出正确的变体选择，并能预测两种变体在循环管线中的内存分配行为。
5. 用 `timeit` 与缓存查询 API 亲手量化两种变体的差别。

## 2. 前置知识

本讲建立在 u3-l1（算子四连函数）与 u2-l4（零拷贝包装）之上，先把几个关键概念补齐：

- **四连函数**：CV-CUDA 的每个 Python 算子都注册了 2×2 四个入口 —— 输入是 `Tensor` 还是 `ImageBatchVarShape`（变长批）× 输出是库分配还是调用者预分配。本讲专门拆解后一个维度。
- **异步提交**：算子调用只是把 CUDA kernel 提交到流（stream）上就立即返回，CPU 侧不等 GPU 算完。所以「算子调用耗时」大部分是 **CPU 侧的提交开销**，这正是两种变体产生差异的地方。
- **对象缓存（object cache）**：CV-CUDA Python 层有一个内部缓存。由 CV-CUDA 分配的对象（`Tensor`、`Image`、`ImageBatchVarShape` 等）在 Python 引用归零后**不真正释放显存**，而是按「shape + dtype + layout」为键存进缓存，下次创建同规格对象时直接复用。注意：**只有 Python 层有缓存，C/C++ 层没有**（u2-l4 已提过：包装外部内存的对象不占缓存额度）。
- **引用计数即"是否在用"**：缓存判断一个缓存项能否被复用，靠的是 C++ `shared_ptr` 的引用计数——只有缓存自己和一个临时引用持有它时才算"空闲"。
- **cudaMalloc 的代价**：CUDA 显存分配是相对昂贵的操作（可能触发驱动级同步）；缓存的第一使命就是让稳态循环里不再出现它。

一个直觉性的成本模型（CPU 侧每次调用）：

\[ T_{\text{allocating}} \approx T_{\text{submit}} + T_{\text{cache lookup}} + P_{\text{miss}} \cdot T_{\text{cudaMalloc}} \]

\[ T_{\text{into}} \approx T_{\text{submit}} \]

其中 \( P_{\text{miss}} \) 是缓存未命中的概率：稳态同形状循环里趋近 0（第一次分配之后），形状每轮都变时趋近 1。本讲要做的就是把这两个公式逐项对应到源码。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [python/mod_cvcuda/operators/OpFlip.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp) | Flip 算子的 pybind11 绑定：四个入口函数与注册代码，本讲的解剖标本 |
| [docs/sphinx/advanced/operator_variants.rst](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/operator_variants.rst) | 官方文档：两种变体的定义、选择建议与约束 |
| [docs/sphinx/advanced/object_cache.rst](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst) | 官方文档：对象缓存机制（复用、增长控制、多线程） |
| [python/mod_cvcuda/nvcv/Tensor.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp) | Python 层 `Tensor::Create` 的实现：缓存查找的入口 |
| [python/mod_cvcuda/nvcv/Cache.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp) | 缓存本体：fetch/add、限额、线程局部存储、Python 查询 API |
| [python/mod_cvcuda/operators/VarShapeUtils.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/VarShapeUtils.hpp) | 变长批 allocating 变体的输出构造辅助函数 |
| [python/mod_cvcuda/operators/Operators.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp) | `CreateOperator`：算子对象本身也走缓存（两个变体共有的开销） |

## 4. 核心概念与源码讲解

### 4.1 两种变体的语义差异：flip 与 flip_into

#### 4.1.1 概念说明

官方文档给每个 Python 算子定义了两种形式：

- **allocating 变体**：`cvcuda.<op>(src, ...)` —— 库为你创建并返回一个全新的输出张量。
- **预分配变体**：`cvcuda.<op>_into(dst, src, ...)` —— 算子直接写入调用者提供的输出张量 `dst`，返回值就是传入的 `dst`。

对应文档原文：[docs/sphinx/advanced/operator_variants.rst:L22-L25](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/operator_variants.rst#L22-L25)（定义两种变体）、[docs/sphinx/advanced/operator_variants.rst:L46-L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/operator_variants.rst#L46-L53)（`_into` 不查缓存、不分配显存、返回传入的张量）。

对调用者来说，语义上的三个直接后果：

1. allocating 版**每一步都会产生新的 Python 对象**，旧输出在失去引用后回到缓存；`_into` 版反复写**同一块**显存。
2. `_into` 版要求 `dst` 的 shape / layout / dtype 与算子"本应产出"的完全一致，否则抛异常（见 4.4）。
3. 两者都把 kernel 提交到同一条流上，计算结果完全相同——差别只在输出的来历。

#### 4.1.2 核心流程

两种变体的调用流程只差一步：

```text
cvcuda.flip(src, 1)                     cvcuda.flip_into(dst, src, 1)
        │                                        │
        ├─ Tensor::Create(shape, dtype)          │  （跳过：dst 由调用者事先给好）
        │    └─ 缓存查找 → 命中复用 / 未命中新建   │
        ▼                                        ▼
        └──────────────► 共同的 FlipInto 路径 ◄───┘
                          │
                          ├─ 未指定 stream 则取 Stream::Current()
                          ├─ CreateOperator<cvcuda::Flip>(0)   ← 算子对象也走缓存
                          ├─ ResourceGuard：input 加 READ 锁、output 加 WRITE 锁
                          └─ Flip->submit(stream, input, output, flipCode)
                                     └─ 返回 output
```

关键观察：**allocating 版本在源码里就是 `_into` 版本的一层薄包装**——先造出输出，再原封不动地调用 `_into` 逻辑。

#### 4.1.3 源码精读

先看 `_into` 的本体，函数 `FlipInto`：

```cpp
Tensor FlipInto(Tensor &output, Tensor &input, int32_t flipCode, std::optional<Stream> pstream)
{
    if (!pstream)
    {
        pstream = Stream::Current();
    }

    auto Flip = CreateOperator<cvcuda::Flip>(0);

    ResourceGuard guard(*pstream);
    guard.add(LockMode::LOCK_MODE_READ, {input});
    guard.add(LockMode::LOCK_MODE_WRITE, {output});
    guard.add(LockMode::LOCK_MODE_NONE, {*Flip});

    guard.run([&Flip, &pstream, &input, &output, &flipCode]()
              { Flip->submit(pstream->cudaHandle(), input, output, flipCode); });

    return output;
}
```

[python/mod_cvcuda/operators/OpFlip.cpp:L35-L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L35-L53) —— `_into` 的全部逻辑：解析当前流、获取算子对象、用 ResourceGuard 给输入加读锁/输出加写锁（保证流式语义下的顺序正确，u4-l1 展开）、提交、`return output`（把传入的 dst 原样返回）。

再看 allocating 版本 `Flip`：

```cpp
Tensor Flip(Tensor &input, int32_t flipCode, std::optional<Stream> pstream)
{
    Tensor output = Tensor::Create(input.shape(), input.dtype());

    return FlipInto(output, input, flipCode, pstream);
}
```

[python/mod_cvcuda/operators/OpFlip.cpp:L55-L60](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L55-L60) —— allocating 变体只比 `_into` 多做一件事：`Tensor::Create` 造输出（内部走缓存，见 4.2），然后**直接委托给 FlipInto**。差异就集中在这一行。

一个值得注意的细节：flip 的 `Tensor::Create` 只传了 `shape` 和 `dtype`，**没有传 layout**（对照 resize 的写法：[python/mod_cvcuda/operators/OpResize.cpp:L62-L67](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResize.cpp#L62-L67) 中是 `Tensor::Create(out_shape, input.dtype(), input.shape().layout())`，多传了第三参）。因此 flip 的 allocating 输出张量 layout 为 `NONE`，而你自己预分配的 `dst` 可以带 NHWC layout。这是两个变体一个可观察的差别。

最后看注册代码，确认两个入口是**同名不同参**的重载决议（u3-l1 的"四连函数"）：

```cpp
m.def("flip", NvtxTrace("cvcuda.flip", &Flip), "src"_a, "flipCode"_a, ...);
m.def("flip_into", NvtxTrace("cvcuda.flip_into", &FlipInto), "dst"_a, "src"_a, "flipCode"_a, ...);
```

[python/mod_cvcuda/operators/OpFlip.cpp:L96-L111](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L96-L111)（`flip`，Tensor 版）、[python/mod_cvcuda/operators/OpFlip.cpp:L113-L129](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L113-L129)（`flip_into`，Tensor 版）。docstring 明确写着返回值是 "The output tensor (same as dst)"。

#### 4.1.4 代码实践：亲眼确认 `_into` 写的就是你的 dst

1. **实践目标**：验证 `_into` 直接写入调用者提供的张量，而 allocating 返回新对象。
2. **操作步骤**（示例代码，待本地验证）：

```python
import cvcuda, numpy as np

src = cvcuda.Tensor((1, 4, 4, 3), np.uint8, cvcuda.TensorLayout.NHWC)
dst = cvcuda.Tensor((1, 4, 4, 3), np.uint8, cvcuda.TensorLayout.NHWC)

r = cvcuda.flip_into(dst, src, 1)      # 左右翻转；若用了显式流，读回前先 stream.sync()

# 直接从 dst 读回（不经过返回值 r），若内容已是翻转结果，则证明写入的就是 dst
host = dst.cuda().cpu() if hasattr(dst.cuda(), "cpu") else np.asarray(dst.cuda())
print(host.shape)

out = cvcuda.flip(src, 1)               # allocating 版
print(out.shape, out.dtype, out.layout) # 观察 layout 是否为 NONE
print(src.layout)                       # 对照：输入是 NHWC
```

3. **需要观察的现象**：`dst` 的内容在无人显式赋值的情况下变成了翻转结果；`out.layout` 打印为 `NONE`（对应上面"没传 layout"的源码细节），而 `dst.layout` 保持 NHWC。
4. **预期结果**：`_into` 写入 dst 本体；allocating 输出的 layout 为 NONE。本环境无 GPU，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：不看源码，如何只用 Python 现象区分两个变体？
**答案**：连续调用 `id(cvcuda.flip(src, 1))` 观察输出对象——allocating 版每次返回新 Python 对象（底层显存来自缓存复用）；`_into` 版永远返回传入的 dst，可以再对比 `out.layout`：allocating 的 flip 输出 layout 是 NONE，自备 dst 可以是 NHWC。

**练习 2**：`cvcuda.flip_into` 的返回值有什么用？既然写的是 dst，为什么不设计成返回 None？
**答案**：返回 dst（源码 `return output;`，[OpFlip.cpp:L52](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L52)）让调用可以链式书写，如 `cvcuda.remap(cvcuda.flip_into(dst, src, 1), map, ...)`，与 allocating 版的用法保持同构，四连函数可以无缝替换。

**练习 3**：两个变体提交 kernel 用的流一样吗？
**答案**：一样。都经由 `FlipInto`：显式传 `stream=` 就用该流，否则 `Stream::Current()`（[OpFlip.cpp:L37-L40](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L37-L40)）。变体只决定输出来历，不改变执行模型。

### 4.2 allocating 的代价：Tensor::Create 走过的缓存路径

#### 4.2.1 概念说明

allocating 变体多出的那一步 `Tensor::Create` 并不等于"每次都 cudaMalloc"。它的真实流程是：拿规格（Requirements）当键去**对象缓存**里找一个空闲的同规格张量；命中就复用，未命中才真正分配并随即登记进缓存。官方文档的表述：[docs/sphinx/advanced/operator_variants.rst:L29-L41](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/operator_variants.rst#L29-L41) —— "每次调用都要发生一次缓存查找或分配"。

所以 allocating 的**每调用固定成本**是一次缓存查找（构造键、哈希、加锁、比对），**偶发成本**是未命中时的 cudaMalloc。理解这一点才能正确预测循环管线的行为（见 4.4）。

#### 4.2.2 核心流程

```text
Tensor::Create(shape, dtype)
   │  CalcRequirements(shape, dtype, 对齐) → Requirements
   ▼
CreateFromReqs(reqs)
   │  Cache::Instance().fetch(Key{reqs})        ← 线程局部缓存，按键取空闲项
   ├── 命中（有空闲项）→ 返回缓存中的张量（不分配显存）
   └── 未命中          → new Tensor(reqs)（真正分配显存）
                         └─ Cache::add(张量)     ← 登记进缓存，供未来复用
```

缓存命中判定依赖"空闲"：缓存项的 `shared_ptr` 引用计数只剩缓存自己 + 判断用的临时引用（计数 ≤ 2）即视为空闲，[python/mod_cvcuda/nvcv/Cache.cpp:L78-L84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L78-L84)。

#### 4.2.3 源码精读

`Tensor::Create` 与缓存查找的入口：

```cpp
std::shared_ptr<Tensor> Tensor::CreateFromReqs(const nvcv::Tensor::Requirements &reqs)
{
    std::vector<std::shared_ptr<CacheItem>> vcont = Cache::Instance().fetch(Key{reqs});

    // None found?
    if (vcont.empty())
    {
        std::shared_ptr<Tensor> tensor(new Tensor(reqs));
        Cache::Instance().add(*tensor);
        return tensor;
    }
    else
    {
        auto tensor = std::static_pointer_cast<Tensor>(vcont[0]);
        ...
```

[python/mod_cvcuda/nvcv/Tensor.cpp:L85-L101](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L85-L101) —— allocating 变体的核心：先 `fetch`，空了才 `new Tensor` 并 `add` 入缓存；其上游 `Tensor::Create`（[Tensor.cpp:L71-L83](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L71-L83)）负责把 shape/dtype/rowalign 换算成 Requirements。

缓存的 `fetch`（跳过仍在使用的项）：

```cpp
std::vector<std::shared_ptr<CacheItem>> Cache::fetch(const IKey &key) const
{
    ...
    auto [firstItem, lastItem] = pimpl->items.equal_range(&key);
    for (auto it = firstItem; it != lastItem; ++it)
    {
        if (!it->second->isInUse())
        {
            v.emplace_back(it->second);
        }
    }
    return v;
}
```

[python/mod_cvcuda/nvcv/Cache.cpp:L224-L243](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L224-L243) —— 用 `unordered_multimap` 的 `equal_range` 按键找出所有候选，只返回"不在使用"的。数据结构本身解释了每次查找的成本构成：哈希 + 锁 + 逐项引用计数检查。

两个影响行为的属性：

- **缓存是线程局部的**：`Cache::Instance()` 是 `thread_local`（[python/mod_cvcuda/nvcv/Cache.cpp:L376-L380](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L376-L380)），A 线程释放的张量 B 线程拿不到。
- **每设备限额默认为总显存的一半**，超限自动清空该设备的缓存项，大于限额的对象根本不入缓存：[python/mod_cvcuda/nvcv/Cache.cpp:L434-L441](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L434-L441)（初始化为 `total_mem / 2`）与 [python/mod_cvcuda/nvcv/Cache.cpp:L151-L183](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L151-L183)（`add` 中的限额检查与驱逐）。

还有一个容易被忽略的对照点：`FlipInto` 里的 `CreateOperator<cvcuda::Flip>(0)` **也走同一个缓存**——算子对象本身以构造参数为键复用，[python/mod_cvcuda/operators/Operators.hpp:L192-L224](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L192-L224)。也就是说：**两个变体共享这部分开销**，allocating 独有的增量只有输出张量那一次 `Tensor::Create`。

#### 4.2.4 代码实践：让缓存行为现形

1. **实践目标**：用 Python 查询 API 观察缓存项数与字节数，验证 allocating 循环"第一次分配、之后复用"。
2. **操作步骤**（示例代码，待本地验证）：

```python
import cvcuda, numpy as np

print("items:", cvcuda.cache_size(), "bytes:", cvcuda.current_cache_size_inbytes())

src = cvcuda.Tensor((8, 224, 224, 3), np.uint8, cvcuda.TensorLayout.NHWC)

for i in range(3):
    out = cvcuda.flip(src, 1)   # 上一轮的 out 在重绑定时失去引用 → 回缓存
    print(i, "items:", cvcuda.cache_size(),
             "bytes:", cvcuda.current_cache_size_inbytes())

cvcuda.clear_cache()            # 手动清空缓存
print("after clear:", cvcuda.cache_size())
```

3. **需要观察的现象**：第 0 轮之后缓存的 bytes 增加约 \( 8 \times 224 \times 224 \times 3 \) 字节量级（加上行对齐的 stride 差异）；第 1、2 轮 bytes 不再增长（复用同一块）；`clear_cache` 后归零。
4. **预期结果**：如上。API 定义见 [python/mod_cvcuda/nvcv/Cache.cpp:L449-L505](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L449-L505)（`clear_cache` / `cache_size` / `current_cache_size_inbytes` 等）。官方同款示例：[samples/object_cache/reuse.py:L18-L42](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/reuse.py#L18-L42)、[samples/object_cache/control.py:L18-L30](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/control.py#L18-L30)。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：循环里 `out = cvcuda.flip(src, 1)` 跑 1000 次，会发生 1000 次 cudaMalloc 吗？
**答案**：不会。第 1 次未命中分配并入缓存；此后每轮 `out` 被重新绑定时，旧输出张量引用归零回到缓存，下一次 `Tensor::Create` 用同样的键 `fetch` 命中它（[Tensor.cpp:L87](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L87)）。稳态成本是每轮一次缓存查找，不是一次分配。

**练习 2**：如果把循环改成 `results.append(cvcuda.flip(src, 1))`，显存会怎样？
**答案**：每轮输出都被列表持有、引用计数降不下来，缓存判定 `isInUse` 为真无法复用，于是每轮都未命中、都分配新显存，总占用线性增长。这不是缓存的 bug，恰恰是"输出被长期持有"时 allocating 变体的固有代价。

**练习 3**：为什么说 `_into` 的内存行为是"确定性的"？
**答案**：它既不查缓存也不分配（[operator_variants.rst:L46-L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/operator_variants.rst#L46-L53)），显存占用在写循环之前就固定为 `dst` 那一块，不依赖缓存命中率、限额驱逐这些运行时状态——对需要精确控制显存预算的服务尤其重要。

### 4.3 变长批的 allocating 更重：CreateSameShapeImageBatch

#### 4.3.1 概念说明

固定形状 `Tensor` 的 allocating 只多一次缓存查找；但 `ImageBatchVarShape`（变长批，u2-l3）的 allocating 变体要做的事多得多：它得为批里**每一张图**分别创建一个 `Image` 再 `pushBackImage` 拼出新批。批越大、图越多，这笔 CPU 侧开销线性放大。

#### 4.3.2 核心流程

```text
FlipVarShape(input)
   └─ CreateSameShapeImageBatch(input)      ← 不含任何缓存感知的整批重建
        ├─ ImageBatchVarShape::Create(capacity)
        └─ for 每张图 i:
             output.pushBackImage(Image::Create(input[i].size(), input[i].format()))
   然后 FlipVarShapeInto(output, input, ...)
```

#### 4.3.3 源码精读

```cpp
Tensor FlipVarShape(...)  // 变长批 allocating 入口
{
    ImageBatchVarShape output = CreateSameShapeImageBatch(input);
    return FlipVarShapeInto(output, input, flipCode, pstream);
}
```

[python/mod_cvcuda/operators/OpFlip.cpp:L83-L88](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L83-L88) —— 变长批 allocating 委托给辅助函数构造输出批，再进入 `FlipVarShapeInto`（[OpFlip.cpp:L62-L81](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L62-L81)，结构与 Tensor 版 `FlipInto` 完全同构）。

辅助函数本体：

```cpp
inline nvcvpy::ImageBatchVarShape CreateSameShapeImageBatch(const nvcvpy::ImageBatchVarShape &input, int capacity)
{
    nvcvpy::ImageBatchVarShape output = nvcvpy::ImageBatchVarShape::Create(capacity);

    for (int i = 0; i < input.numImages(); ++i)
    {
        output.pushBackImage(nvcvpy::Image::Create(input[i].size(), input[i].format()));
    }
    return output;
}
```

[python/mod_cvcuda/operators/VarShapeUtils.hpp:L48-L63](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/VarShapeUtils.hpp#L48-L63) —— 逐图 `Image::Create`（每个 Image 内部又各走一次缓存查找）。注意：这些 Image/Tensor 一样会被缓存复用，所以稳态下依然不是每次 cudaMalloc，但**每次调用的查找次数是 O(批内图数)** 而非 O(1)。

结论：**变长批管线从 `_into` 获得的 CPU 侧收益通常比固定张量批更大**。官方文档也确认 `_into` 模式同样覆盖变长批重载（`cvcuda.<op>_into(dst_batch, src_batch, ...)`）：[operator_variants.rst:L99-L102](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/operator_variants.rst#L99-L102)。

#### 4.3.4 代码实践：数一数缓存项

1. **实践目标**：直观感受变长批 allocating 每次调用产生的对象数。
2. **操作步骤**（示例代码，待本地验证）：构造一个含 16 张小图的变长批，循环调用 `cvcuda.flip(batch, code)` 三次，每次打印 `cvcuda.cache_size()`；再换成预分配 `dst_batch` 的 `flip_into` 重复实验。
3. **需要观察的现象**：allocating 版首轮后缓存项显著增加（每张图一个 Image 项 + 批容器项），`_into` 版缓存项数基本不变。
4. **预期结果**：如上；具体数字**待本地验证**（可用 [samples/datatypes/imagebatchvarshape.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/datatypes/imagebatchvarshape.py) 的方式构造变长批）。

#### 4.3.5 小练习与答案

**练习 1**：变长批 allocating 与 Tensor allocating 的输出构造开销差在哪？
**答案**：Tensor 版一次 `Tensor::Create` → 一次缓存查找（[Tensor.cpp:L85-L101](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L85-L101)）；变长批版要 `Create` 批容器 + 逐图 `Image::Create`（[VarShapeUtils.hpp:L48-L63](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/VarShapeUtils.hpp#L48-L63)），查找/建对象次数随批内图数线性增长。

**练习 2**：既然每个 Image 也走缓存，变长批循环里还会有 cudaMalloc 吗？
**答案**：稳态下不会——上一轮输出批整体失引用后，里面的 Image 逐个回缓存，下一轮逐图命中。代价转化为"每轮 O(N) 次缓存查找 + 多个 Python/C++ 对象的构造与析构"，这正是 `_into` 能省掉的部分。

### 4.4 如何选择：决策规则、约束与陷阱

#### 4.4.1 概念说明

官方文档给出了明确的选择建议（[operator_variants.rst:L55-L70](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/operator_variants.rst#L55-L70)），可整理成决策表：

| 场景 | 推荐变体 | 理由 |
|------|----------|------|
| 一次性脚本 / 原型 | allocating | 省事，不用预先算输出规格 |
| 输出形状每次都变（如目标尺寸随输入分辨率变） | allocating | 缓存替你管理多变的形状 |
| 固定形状推理管线（批量预处理的主流情形） | `_into` | 启动时分配一次，循环内零查找 |
| 紧循环里反复调用同一算子 | `_into` | 消除每次迭代的缓存开销 |
| 自管缓冲池、要求显存行为可预测 | `_into` | 不受缓存限额/驱逐策略影响 |
| 变长批大批量流转 | `_into` | 避免 O(图数) 的逐图建批开销 |

#### 4.4.2 核心流程（`_into` 的约束检查）

`_into` 的自由是有条件的：你提供的 `dst` 必须已经具备算子"本应产出"的 shape / layout / dtype，不匹配会直接抛异常（[operator_variants.rst:L92-L97](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/operator_variants.rst#L92-L97)）。检查发生在算子 priv 层提交之前（u3-l1 讲过 priv 层"先校验后执行"的模式），所以错误在 CPU 侧立刻暴露，不会污染 GPU 数据。

#### 4.4.3 源码精读（陷阱的证据）

三个必须知道的陷阱，全部有官方文档或源码背书：

1. **缓存无界增长**：非包装对象形状五花八门时缓存持续膨胀，达到限额才整设备清空；可用 `set_cache_limit_inbytes` 控制、设 0 相当于禁用缓存（[object_cache.rst:L79-L110](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L79-L110)，限额实现 [Cache.cpp:L286-L327](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L286-L327)）。
2. **不要手动管理释放**：`del` 只删引用，内存留在缓存等复用，最佳实践是不手动释放（[object_cache.rst:L50-L62](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L50-L62)）。
3. **缓存按线程隔离、但限额共享**：多线程程序里一个线程的缓存另一个线程用不上，而限额与统计是全进程共享的（[object_cache.rst:L120-L133](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L120-L133)；`thread_local` 证据 [Cache.cpp:L376-L380](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L376-L380)）。`_into` 天然绕开这三条：它既不进缓存也不查缓存。

#### 4.4.4 代码实践：验证约束

1. **实践目标**：亲眼看到 `_into` 对不匹配 dst 的拒绝行为。
2. **操作步骤**（示例代码，待本地验证）：

```python
import cvcuda, numpy as np

src = cvcuda.Tensor((8, 224, 224, 3), np.uint8, cvcuda.TensorLayout.NHWC)
bad = cvcuda.Tensor((8, 128, 128, 3), np.uint8, cvcuda.TensorLayout.NHWC)  # 形状不符
cvcuda.flip_into(bad, src, 1)   # 预期抛异常
```

3. **需要观察的现象**：抛出异常，指出输出规格不匹配。
4. **预期结果**：异常在 CPU 侧（priv 层校验）立即抛出。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：视频解码管线每帧分辨率都可能不同，该用哪个变体？
**答案**：输入侧用变长批承载天然不同尺寸；但如果下游推理要求固定输入（如 224×224），输出形状其实是固定的——预分配一个固定输出批/张量用 `_into` 最优。只有输出形状真正多变时才让 allocating + 缓存兜底。

**练习 2**：`_into` 能否节省峰值显存？
**答案**：在"稳态同形状、旧输出及时失引用"的循环里，allocating 靠缓存复用，峰值显存与 `_into` 接近；`_into` 的优势是**确定性**——显存用量在循环前就锁定，且省掉 CPU 侧查找。但当输出被持有（见 4.2 练习 2）或形状多变导致缓存未命中时，allocating 的峰值显存和分配次数都会明显升高。

**练习 3**：`cvcuda.set_cache_limit_inbytes(0)` 之后 allocating 循环会变慢吗？
**答案**：会明显变慢：限额为 0 时对象不入缓存（[Cache.cpp:L158-L161](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L158-L161) 中"大于限额不入缓存"，0 限额即全部拒绝），每次 `Tensor::Create` 都未命中 → 每次循环一次 cudaMalloc/free。而 `_into` 完全不受影响——这也是两者实现路径差异的最直接实验证据。

## 5. 综合实践

**任务**：用 `timeit` 定量对比两种变体，并解释每一微秒差异的来源。以下为完整脚本（示例代码，**待本地验证**——本环境无 GPU）：

```python
# compare_variants.py
import timeit
import cvcuda
import numpy as np

BATCH, H, W, C = 8, 224, 224, 3
N = 1000

src = cvcuda.Tensor((BATCH, H, W, C), np.uint8, cvcuda.TensorLayout.NHWC)
dst = cvcuda.Tensor((BATCH, H, W, C), np.uint8, cvcuda.TensorLayout.NHWC)  # 预分配

def alloc_loop():
    out = cvcuda.flip(src, 1)          # out 下轮重绑定即失引用 → 回缓存

def into_loop():
    cvcuda.flip_into(dst, src, 1)

# 预热：触发首次分配、让缓存就位、CUDA 上下文就绪
alloc_loop(); into_loop()

t_alloc = timeit.timeit(alloc_loop, number=N) / N * 1e6
t_into  = timeit.timeit(into_loop,  number=N) / N * 1e6

print(f"allocating : {t_alloc:8.2f} us/call")
print(f"_into      : {t_into:8.2f} us/call")
print(f"差值        : {t_alloc - t_into:8.2f} us/call")
print(f"cache items: {cvcuda.cache_size()}, "
      f"bytes: {cvcuda.current_cache_size_inbytes()}")

# 进阶实验 A：禁用缓存后再测 allocating，观察它退化成每次 cudaMalloc
cvcuda.set_cache_limit_inbytes(0)
t_alloc_nocache = timeit.timeit(alloc_loop, number=N) / N * 1e6
print(f"allocating(no cache): {t_alloc_nocache:8.2f} us/call")

# 进阶实验 B：换成变长批输入再测一轮，验证 4.3 的 O(图数) 结论
```

**操作步骤**：

1. 在装有 cvcuda wheel 的 GPU 机器上保存并运行 `python compare_variants.py`。
2. 记录三组数字：默认缓存下的 allocating、`_into`、禁用缓存后的 allocating。
3. （可选）峰值显存观察：由于 cvcuda 的分配走自己的 nvcv 分配器而**不经过** PyTorch 的分配器，`torch.cuda.max_memory_allocated()` 看不到 cvcuda 的显存——请改用 `cvcuda.current_cache_size_inbytes()`（本脚本已用）加终端里 `watch -n 0.2 nvidia-smi` 轮询配合。

**需要观察的现象与预期结果**（待本地验证）：

1. 默认缓存下 `allocating` 略慢于 `_into`，差值为每次调用一次缓存查找（键构造 + 哈希 + 锁 + 引用计数检查）的 CPU 开销；因为两个算子都是异步提交，测得的差值主要来自 CPU 侧。
2. 禁用缓存后 allocating 显著变慢（每轮多一次 cudaMalloc/free 的同步代价），而 `_into` 若再测一次应基本不变——这一组对照是两种变体实现路径差异的最有力证据。
3. `cache_size` / `current_cache_size_inbytes` 在稳态循环中保持恒定，印证"第一次分配、之后复用"。

**结论模板**（用你测到的数字填充）：

> 在 (GPU 型号) 上，N=1000 次循环中 allocating 比 `_into` 慢 x.x μs/次（约 x%）；禁用缓存后差距扩大到 x.x μs/次。原因是 allocating 每次调用额外执行 ______，缓存未命中时还要 ______；`_into` 则完全绕开缓存，直接写入预分配的 dst。

## 6. 本讲小结

- 每个算子都有两种变体：allocating（`cvcuda.flip(src, ...)`）创建并返回新输出；`_into`（`cvcuda.flip_into(dst, src, ...)`）直接写入调用者的 dst 并将其返回。
- 源码上 allocating 就是 `_into` 的薄包装：多一行 `Tensor::Create`，然后原样委托 `FlipInto`（[OpFlip.cpp:L55-L60](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L55-L60)）。
- `Tensor::Create` 先按 Requirements 查线程局部的对象缓存，命中复用、未命中才分配并入缓存（[Tensor.cpp:L85-L101](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L85-L101)）；`_into` 完全不碰缓存。
- 算子对象本身（`CreateOperator`）也走缓存，且为两个变体共有——allocating 的独有增量只有输出张量的创建路径。
- 变长批的 allocating 要逐图 `Image::Create` 重建输出批，开销 O(批内图数)，从 `_into` 获益更大。
- 选择口诀：原型/形状多变用 allocating；固定形状生产管线、紧循环、需要确定性显存行为用 `_into`，且 dst 必须与算子产出规格完全一致。

## 7. 下一步学习建议

- **下一讲 u3-l4（融合算子）**：把 resize+crop+cvtcolor 合成一个 kernel，是 `_into` 预分配思想在"减少 kernel 启动与显存读写"上的进一步延伸，学完后可以把本讲的计时脚本扩展到融合算子。
- **u4-l1（Stream 执行模型）**：本讲反复出现的 `Stream::Current()` 与 ResourceGuard 读/写锁将在那里展开——理解为什么输出写入前要加 WRITE 锁。
- **u4-l2（对象缓存）**：本讲只拆了缓存的查找路径；缓存的命中条件细节、`unbounded_growth` 实验与多线程行为是专题内容。
- **延伸阅读源码**：[python/mod_cvcuda/nvcv/Cache.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp) 的 `add`/`fetch`/`setCacheLimit`，对照 `samples/object_cache/` 目录的 7 个示例逐个运行。

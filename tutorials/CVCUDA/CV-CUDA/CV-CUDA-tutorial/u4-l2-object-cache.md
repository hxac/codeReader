# Python 对象缓存：Tensor/ImageBatch 的自动复用

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 CV-CUDA 对象缓存（object cache）的准确边界：它**只存在于 Python 绑定层**，C/C++ API 完全没有这套机制。
2. 描述一次缓存命中的全部条件：键匹配（shape 含 layout、dtype、所在 GPU 设备）且对象当前未被使用。
3. 区分「非包装对象」与「包装对象」在缓存里的两种截然不同的待遇：前者占配额、可复用显存；后者零字节记账、进缓存只为保活（Image 外壳还可复用）。
4. 解释 `del tensor` 之后显存为什么不下降，以及什么时候内存才真正归还。
5. 复现 unbounded growth（缓存无界增长）场景，理解默认「半张卡显存」配额与「超限全清」的锯齿式淘汰策略，并会用 `set_cache_limit_inbytes` / `clear_cache` 控制。

本讲是第四单元第二讲。上一讲（u4-l1）我们确立了「一切算子都异步提交到流上」的执行模型；本讲回答的是与之配套的另一半问题：**异步世界里的显存何时才能安全回收、如何高效复用**。这两讲合起来，就是 CV-CUDA Python 层正确性与性能的两大支柱。

## 2. 前置知识

### 2.1 引用计数与 shared_ptr

Python 对象靠引用计数管理生命周期：引用归零即析构。C++ 侧对应物是 `std::shared_ptr`——多个 `shared_ptr` 指向同一对象，每多一个引用计数加一，最后一个引用消失时对象析构、内存释放。`use_count()` 返回当前引用数。本讲会反复用到这个概念，因为**缓存本身就是一个长期持有 `shared_ptr` 的「额外引用」**。

### 2.2 哈希表与 multimap

缓存本质是一张哈希表。CV-CUDA 用的是 `std::unordered_multimap`：普通 map 中一个键只对应一个值，而 multimap 允许**同一个键挂多个条目**——比如你同时持有 3 个同形状张量，它们先后闲置后，缓存里同键条目就有 3 个。哈希表查找分两步：先用 `hash()` 把键映射到桶（快速定位），再用 `operator==` 在桶内做精确比较（处理哈希碰撞）。因此「键相等」的判定逻辑就是缓存命中的判定逻辑。

### 2.3 thread_local 与互斥锁

`thread_local` 修饰的变量**每个线程各有一份**，互不干扰；`std::mutex` 则是跨线程的排他锁。CV-CUDA 的缓存实例是 thread_local 的（每个线程一张自己的哈希表），但配额记账（已用字节数、上限）是全局共享的、用锁保护。这个「表按线程分、账全局记」的设计是理解多线程行为的关键。

### 2.4 承接前面几讲

- u2-l1/u2-l4：张量由 shape、dtype、layout 描述；`as_tensor` 包装外部显存得到的是 wrapped tensor，它不拥有内存。
- u3-l3：allocating 变体（`cvcuda.flip`）每次调用会隐式 `Tensor::Create` 创建输出——本讲会看到这一步其实先查缓存。
- u4-l1：算子异步提交到流，`ResourceGuard` 会在对象析构时通过事件保证流上的工作先完成。缓存持有的引用同样要过这道闸，所以「放进缓存」与「流安全」是联动的。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [python/mod_cvcuda/nvcv/Cache.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.hpp) | 缓存条目抽象 `CacheItem`、外部条目 `ExternalCacheItem`、`Cache` 类声明 |
| [python/mod_cvcuda/nvcv/Cache.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp) | 缓存核心实现：增删查、配额、淘汰、Python API 导出 |
| [python/mod_cvcuda/include/nvcv/python/Cache.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/Cache.hpp) | 公共头：键的基类 `IKey`（设备捕获、hash/相等协议） |
| [python/mod_cvcuda/nvcv/Tensor.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp) | Tensor 的缓存键定义与「先查缓存再创建」路径、包装路径 |
| [python/mod_cvcuda/nvcv/ImageBatch.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageBatch.cpp) | ImageBatchVarShape 的缓存键（capacity）与创建路径 |
| [python/mod_cvcuda/nvcv/Image.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Image.cpp) | 包装 Image 的外壳复用路径 |
| [docs/sphinx/advanced/object_cache.rst](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst) | 官方文档：wrapped/non-wrapped、del 与 GC、配额、多线程 |
| [samples/object_cache/](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/basic.py) 下 7 个样例 | basic / basic_wrapped / reuse / unbounded_growth / control / control_torch / threads，全部是文档的 literalinclude 素材 |
| [tests/cvcuda/python/test_cache.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py) | 官方测试，用断言固定了缓存的可观察行为 |

## 4. 核心概念与源码讲解

### 4.1 缓存骨架：CacheItem、IKey 与线程局部 Cache 实例

#### 4.1.1 概念说明

CV-CUDA 的 Python 层内置了一个资源管理系统：凡是它创建的 `Tensor`、`Image`、`ImageBatchVarShape`、`TensorBatch`、`Array`、`Stream`，以及算子对象，都会被自动登记进一张缓存表。官方文档开宗明义地划出两条边界：

- [docs/sphinx/advanced/object_cache.rst:25-27](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L25-L27) 明确注明：**只有 Python 对象会被缓存，C/C++ 对象没有缓存**。
- [docs/sphinx/advanced/object_cache.rst:28-29](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L28-L29) 注明：CV-CUDA 与设备无关，**不追踪数据实际躺在哪块 GPU 上**（这个设计在多 GPU 一节会再遇到）。

为什么需要缓存？因为在 GPU 上 `cudaMalloc` 是昂贵的同步操作（可能引发隐式同步、微秒到毫秒级开销），而视觉管线的典型形态是「同一组形状每帧重复出现」。把闲置对象按规格存起来复用，稳态循环就几乎不再分配显存。这与 PyTorch 的 caching allocator 解决的是同一类问题，但 CV-CUDA 选择在**对象**层面而非裸内存块层面复用。

#### 4.1.2 核心流程

一次「创建对象」在缓存视角下的流程：

```text
Python: cvcuda.Tensor(shape, dtype, layout)
        │
        ▼
构造缓存键 Key = (形状+布局, 数据类型, 当前 CUDA 设备号)
        │
        ▼
Cache::fetch(key) ── 在哈希表里找同键且"当前无人使用"的条目
        │
   ┌────┴─────┐
   命中            未命中
   │              │
   ▼              ▼
直接返回缓存对象    new 对象(此刻分配显存)
(零分配)          并 Cache::add 登记进表
```

「当前无人使用」的判定基于引用计数，这是整个缓存最精妙的一行逻辑：

\[ \text{isInUse} \iff \text{use\_count} > 2 \]

数字 2 的来历：判定时刻，缓存表自身持有 1 个 `shared_ptr`，判定函数的局部变量 `sthis` 又临时持有 1 个。除此之外每多一个引用（Python 变量、ResourceGuard 的 GCBag 等）都意味着还有使用方。所以缓存只回收「世界上只剩缓存自己记得它」的对象。

#### 4.1.3 源码精读

先看条目与判定的接口。[python/mod_cvcuda/nvcv/Cache.hpp:36-55](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.hpp#L36-L55) 定义了 `CacheItem`：任何想进缓存的对象必须提供 `key()`（我是谁）和 `GetSizeInBytes()`（我占多少配额）。

[python/mod_cvcuda/nvcv/Cache.cpp:78-84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L78-L84) 就是上面那条引用计数规则：

```cpp
bool CacheItem::isInUse() const
{
    std::shared_ptr<const CacheItem> sthis = this->shared_from_this();
    // Return true if it is being used anywhere apart from cache and sthis
    return sthis.use_count() > 2;
}
```

[python/mod_cvcuda/nvcv/Cache.cpp:86-94](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L86-L94) 是缓存的数据结构：一个 `unordered_multimap`（键可重复），加上三个 `inline static` 成员——全局互斥锁和**按设备记账**的两张表（每个 GPU 各自的配额与当前用量）。

```cpp
using Items = std::unordered_multimap<const IKey *, std::shared_ptr<CacheItem>, HashKey, KeyEqual>;

struct Cache::Impl
{
    Items                                          items;
    inline static std::mutex                       mtx;
    inline static std::unordered_map<int, int64_t> cache_limit_inbytes;
    inline static std::unordered_map<int, int64_t> current_size_inbytes;
};
```

键的公共协议在 [python/mod_cvcuda/include/nvcv/python/Cache.hpp:31-78](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/Cache.hpp#L31-L78)：`IKey` 构造时用 `cudaGetDevice` 捕获当前设备号；`hash()` 把「具体键类型」和「设备号」都混进哈希值，`operator==` 先比类型、再比设备、最后交给子类的 `doIsCompatible`。这保证**不同 GPU 上的条目永不互相命中**。

最后看实例的取得方式，[python/mod_cvcuda/nvcv/Cache.cpp:376-380](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L376-L380)：

```cpp
Cache &Cache::Instance()
{
    thread_local Cache cache;
    return cache;
}
```

一行 `thread_local` 道出多线程语义：**每个线程一张独立的缓存表**。同时构造函数把 `this` 登记进静态 `instances` 集合（[python/mod_cvcuda/nvcv/Cache.cpp:96-101](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L96-L101)），供 `ClearAll`/`TotalSize` 遍历所有线程的表。

#### 4.1.4 代码实践

**实践目标**：用官方 `threads.py` 样例直观看到「表按线程分、计数可全局查」。

**操作步骤**：

1. 环境按 u1-l2 配好（`pip install cvcuda-cu12` 及 samples 依赖）后运行：

   ```bash
   python samples/object_cache/threads.py
   ```

2. 阅读样例时注意 [samples/object_cache/threads.py:29-31](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/threads.py#L29-L31)：子线程里先创建一个张量，打印 `cache_size()`（全局）与 `cache_size(ThreadScope.LOCAL)`（本线程），随后 `clear_cache(ThreadScope.LOCAL)` 清空本线程表再打印一次。样例注释里直接写出了预期输出 `2 1` 与 `1 0`。

3. 顺手读一下 [samples/object_cache/threads.py:42-65](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/threads.py#L42-L65) 那段很长的 WORKAROUND 注释——它解释了 Python `thread.join()` 与 C++ thread_local 析构之间的竞态，是了解「线程局部缓存」实现代价的珍贵材料。

**需要观察的现象**：全局计数 = 主线程条目 + 子线程条目；LOCAL 清空只影响本线程的表。

**预期结果**：输出两行 `2 1` 和 `1 0`。样例自带这些注释，可与实际运行对照。本环境无 GPU，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`isInUse()` 里为什么阈值是 2 而不是 1？

**答案**：判定瞬间有两个「合法」引用：缓存哈希表里的 `shared_ptr` 和 `shared_from_this()` 产生的局部 `sthis`。`use_count() > 2` 意味着除这两者外还有别人（Python 变量、GCBag 等）握着对象，即真在被使用；等于 2 时世界上只剩缓存记得它，可以安全复用。

**练习 2**：为什么缓存要用 `unordered_multimap` 而不是 `unordered_map`？

**答案**：同一个键（同一规格）的对象可以同时存在多个。例如一个循环里同时持有 3 个同形状输出张量，它们全部闲置后都会以相同键挂在缓存里，下次同一形状需要 3 个并发输出时可以全部复用。`unordered_map` 一个键只留一个值，表达不了这种「同规格对象池」。

### 4.2 非包装对象的复用路径：Tensor 与 ImageBatchVarShape 的命中条件

#### 4.2.1 概念说明

「非包装对象」（non-wrapped）指由 CV-CUDA 自己分配显存的对象，比如 `cvcuda.Tensor(...)` 直接构造、或 allocating 算子隐式创建的输出。它们是缓存真正服务的对象：**占配额、按规格键复用**。

每种容器定义自己的键，键的内容就是「命中条件」的全部：

| 容器 | 键 | 命中条件 |
|------|-----|---------|
| `Tensor` | shape + layout + dtype（+设备） | 形状、布局、类型完全一致 |
| `ImageBatchVarShape` / `TensorBatch` | capacity（+设备） | 容量一致即可，批内每图尺寸无关（承接 u2-l3 的结论） |
| `Stream` | 同类键 | 同上 |
| 算子对象（经 `ExternalCacheItem`） | 算子各自定义 | 承接 u3-l3：`CreateOperator` 也走缓存 |

注意 Tensor 的键里 layout 参与匹配是有源码依据的：`TensorShape` 的相等比较同时比较形状和布局（[src/nvcv/src/include/nvcv/TensorShape.hpp:188-191](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorShape.hpp#L188-L191)）。所以 `(16,32,4)` 的 HWC 张量与同形状但 layout 为 `NHWC` 或 `NONE` 的张量**不会**互相命中。

#### 4.2.2 核心流程

以一次 allocating 算子调用为例（这是缓存最主要的使用方）：

```text
cvcuda.flip(src, 1)                      # allocating 变体
  └─ OpFlip.cpp: Tensor::Create(input.shape(), input.dtype())
       └─ Tensor::CreateFromReqs(reqs)
            ├─ Cache::fetch(Key{reqs})   # 键 = (shape+layout, dtype, deviceId)
            │    ├─ 命中(且未在用) → 返回缓存张量，零显存分配
            │    └─ 未命中 → new Tensor(reqs)   # 此刻才 cudaMalloc
            │                └─ Cache::add(*tensor)  # 立即登记进表
            └─ FlipInto(output, ...)     # 写入复用来的输出
```

两个容易忽略的要点：

1. **对象是创建后立即入缓存的**，不是等它「离开作用域」才入缓存。Python 引用消失后对象之所以不析构，是因为缓存早就握着一个 `shared_ptr`。
2. 键相同 ≠ 一定命中。`fetch` 只返回 `!isInUse()` 的条目；如果旧输出还被某处引用着，本次仍会新分配。

#### 4.2.3 源码精读

[python/mod_cvcuda/operators/OpFlip.cpp:55-60](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L55-L60)：allocating 变体的第一行就是 `Tensor::Create`——u3-l3 说过的「隐式分配」，实际入口在这里：

```cpp
Tensor Flip(Tensor &input, int32_t flipCode, std::optional<Stream> pstream)
{
    Tensor output = Tensor::Create(input.shape(), input.dtype());
    return FlipInto(output, input, flipCode, pstream);
}
```

[python/mod_cvcuda/nvcv/Tensor.cpp:85-103](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L85-L103) 是「先查缓存再创建」的完整路径：

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
        // Get the first one
        auto tensor = std::static_pointer_cast<Tensor>(vcont[0]);
        NVCV_ASSERT(tensor->dtype() == reqs.dtype);
        return tensor;
    }
}
```

Tensor 键的定义与比较在两处：[python/mod_cvcuda/nvcv/Tensor.hpp:67-85](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.hpp#L67-L85) 声明键成员为 `m_shape`（含布局）、`m_dtype`、`m_wrapper` 三个字段；[python/mod_cvcuda/nvcv/Tensor.cpp:332-364](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L332-L364) 给出哈希与相等实现——普通键按 `(shape, dtype)` 计算，最终命中判定是 `std::tie(m_shape, m_dtype) == std::tie(that.m_shape, that.m_dtype)`：

```cpp
bool Tensor::Key::doIsCompatible(const IKey &that_) const
{
    const auto &that = static_cast<const Key &>(that_);
    // ...(wrapper 分支见 4.3)...
    return std::tie(m_shape, m_dtype) == std::tie(that.m_shape, that.m_dtype);
}
```

变长批的键则简单得多。[python/mod_cvcuda/nvcv/ImageBatch.cpp:42-52](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageBatch.cpp#L42-L52) 显示 `ImageBatchVarShape::Key` 只由 capacity 构成（因为批容器本身只存指针与元数据，不拥有像素，u2-l3 讲过）；[python/mod_cvcuda/nvcv/ImageBatch.cpp:54-73](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageBatch.cpp#L54-L73) 的 `Create` 走同样的 fetch→复用/新建→add 三段式，命中时还会 `batch->clear()` 把外壳恢复到干净状态。

配额的记账口径在 [python/mod_cvcuda/nvcv/Tensor.cpp:266-279](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L266-L279)：`GetSizeInBytes` 用 `nvcvMemRequirementsCalcTotalSizeBytes` 按 Requirements 算出字节数——注意这是**含 stride 对齐后**的缓冲大小（u2-l1 讲过行距对齐），不是元素数乘 dtype 的理论值。

#### 4.2.4 代码实践

**实践目标**：亲手复现 `reuse.py` 的复用行为，并用计数器证明「命中零分配」。

**操作步骤**（以下为示例代码，保存为自己是新文件，勿改动仓库样例）：

1. 先原样阅读两个样例：[samples/object_cache/basic.py:18-30](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/basic.py#L18-L30)（创建一个非包装张量，缓存 +1）与 [samples/object_cache/reuse.py:18-42](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/reuse.py#L18-L42)（两个函数各创建一个同规格张量，第二个复用第一个的显存）。
2. 写如下脚本（示例代码）：

   ```python
   import cvcuda, numpy as np

   def make(shape):
       return cvcuda.Tensor(shape, np.float32, cvcuda.TensorLayout.HWC)

   cvcuda.clear_cache()
   t1 = make((16, 32, 4))
   print("after t1:", cvcuda.cache_size(), cvcuda.current_cache_size_inbytes())
   del t1                       # 引用消失，但缓存仍持有
   t2 = make((16, 32, 4))       # 同 shape+layout+dtype → 应命中
   print("after t2:", cvcuda.cache_size(), cvcuda.current_cache_size_inbytes())
   t3 = make((16, 32, 4))       # 与 t2 并存 → 键相同但仍需新分配（isInUse）
   print("after t3:", cvcuda.cache_size(), cvcuda.current_cache_size_inbytes())
   ```

3. 对照官方测试 [tests/cvcuda/python/test_cache.py:101-126](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py#L101-L126) 检查你的理解：它用 `cvcuda.internal.nbytes_in_cache(obj)`（导出自 [python/mod_cvcuda/nvcv/Cache.cpp:507-508](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L507-L508)）逐对象核对累计字节数。

**需要观察的现象**：`t2` 创建前后 `current_cache_size_inbytes()` 不变（复用，字节数不增）；`t3` 创建后字节数增加（同键但 t2 在用，须新分配）。

**预期结果**：计数呈 `+1 条目 / 字节不变 / 字节翻倍` 的模式。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`del t1` 之后 `nvidia-smi` 里显存占用会下降吗？什么时候才下降？

**答案**：不会。`del` 只移除 Python 引用，缓存表里的 `shared_ptr` 仍持有对象。真正归还发生在：`cvcuda.clear_cache()` 被调用、缓存超限触发整体淘汰、脚本退出时的 `RegisterCleanup` 钩子（[python/mod_cvcuda/nvcv/Cache.cpp:446-447](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L446-L447)），或对象大到超过配额根本不入缓存。

**练习 2**：`(16, 32, 4)` 的 HWC 张量释放后，创建 `(16, 32, 4)` 但 layout 为 `NHWC` 的张量能命中吗？

**答案**：不能。Tensor 的键比较走 `TensorShape::operator==`，它同时比较形状与布局（TensorShape.hpp L188-191），HWC ≠ NHWC，键不相等即不命中。dtype 不同（如 float32 换 uint8）同理。

**练习 3**：为什么 `ImageBatchVarShape` 的键只看 capacity，而 Tensor 的键要看完整 shape？

**答案**：批容器是句柄式容器，自身只存每图的指针与元数据（u2-l3），内存开销只取决于容量，与批内图像尺寸无关；而 Tensor 直接拥有整块像素缓冲，缓冲大小由 shape/dtype/对齐决定，必须精确匹配才能安全复用。

### 4.3 包装对象：零配额、保活与外壳复用

#### 4.3.1 概念说明

「包装对象」（wrapped）指 `as_tensor` / `as_image` 包装外部框架显存得到的对象（u2-l4 讲过：元数据抄入、只登记不分配）。它们的显存归 torch/cupy/numpy 所有，CV-CUDA 无权也无法复用。因此缓存对它们的策略完全不同：

- **零字节记账**：包装张量的 `GetSizeInBytes()` 为 0，完全不占缓存配额。官方文档 [docs/sphinx/advanced/object_cache.rst:43-48](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L43-L48) 的表述是「包装对象不增加缓存占用」。
- **仍然进缓存，但目的是保活**：算子是异步提交到流上的（u4-l1）。如果 Python 侧引用先消失、而流上的 kernel 还没跑到用它的那一步，对象就会在半路被析构。缓存多持一个引用，相当于给「流上的最后一位使用者」兜底。
- **包装 Image 还有一层「外壳复用」**：构造 `nvcvpy::Image` 外壳昂贵（其 Resource 基类要创建 `cudaEvent_t` 等资源），所以复用的是外壳，装进新的 buffer 元数据——这正是文档第 77 行「缓存复用同样适用于包装对象」的准确含义：**复用的是 Python/C++ 外壳对象，不是显存**。

#### 4.3.2 核心流程

包装张量与包装图像的路径略有不同：

```text
as_tensor(torch_tensor)                 as_image(buffer, fmt)
  └─ Tensor::Wrap                         └─ Image 包装路径
       ├─ 构造 wrapper 键                      ├─ 构造 wrapper 键
       ├─ removeAllNotInUseMatching(wrapper键) ├─ fetchOne(wrapper键)   ← 外壳复用!
       │   (旧 wrapper 不可复用，清掉)          │    命中 → setWrapData(换新 buffer 元数据)
       ├─ new Tensor(data, buffer引用)         │    未命中 → new Image + add
       └─ Cache::add  ← 仅保活                 └─ removeAllNotInUseMatching(清理其余闲置外壳)
```

#### 4.3.3 源码精读

[python/mod_cvcuda/nvcv/Tensor.cpp:168-203](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L168-L203) 是 `Tensor::Wrap`。注意第 174-179 行的注释与第 198-201 行的注释——前者说明所有 wrapper 共用一个键，创建时顺手把**不在使用的** wrapper 从缓存清掉（它们反正不可复用）；后者说明把新 wrapper 加进缓存的原因是防止「流还在用、Python 已放手」的悬空：

```cpp
    // This is the key of a tensor wrapper.
    // All tensor wrappers have the same key.
    Tensor::Key key;
    // We take this opportunity to remove from cache all wrappers that aren't
    // being used. They aren't reusable anyway.
    Cache::Instance().removeAllNotInUseMatching(key);
    // ...
    // Need to add wrappers to cache so that they don't get destroyed by
    // the cuda stream when they're last used, and python script isn't
    // holding a reference to them. If we don't do it, things might break.
    Cache::Instance().add(*tensor);
```

wrapper 键「全部相等」的实现见 [python/mod_cvcuda/nvcv/Tensor.cpp:332-358](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L332-L358)：wrapper 键哈希恒为 0、互相比较恒为真——不是为了让它们命中复用，而是为了让 `removeAllNotInUseMatching` 一次捞出全部闲置 wrapper。

零字节记账的机制藏在一个不起眼的构造函数里。[python/mod_cvcuda/nvcv/Tensor.cpp:246-251](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L246-L251)：包装版构造函数用**默认构造的空 Requirements** 计算大小，得到的字节数自然是 0：

```cpp
Tensor::Tensor(const nvcv::TensorData &data, py::object wrappedObject)
    : m_impl{nvcv::TensorWrapData(data)}
    , m_size_inbytes{doComputeSizeInBytes(nvcv::Tensor::Requirements())}  // == 0
    , m_wrappedObject(wrappedObject)
```

官方测试为这个行为背书：[tests/cvcuda/python/test_cache.py:128-135](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py#L128-L135) 用 cupy 数组 `as_tensor` 包装后断言 `current_cache_size_inbytes() == 0`。

包装 Image 的外壳复用在 [python/mod_cvcuda/nvcv/Image.cpp:676-706](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Image.cpp#L676-L706)：`fetchOne` 拿到一个闲置外壳后调 `setWrapData` 换上新的 buffer 元数据。为什么值得这么做？[python/mod_cvcuda/nvcv/Image.cpp:743-745](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Image.cpp#L743-L745) 的注释给出答案：

```cpp
//We recreate the nvcv::Image wrapper (m_impl) because it's cheap.
//It's not cheap to create nvcvpy::Image as it might have allocated expensive resources (cudaEvent_t in Resource parent).
```

最后补一块拼图：算子对象也进缓存，但走 `ExternalCacheItem`（[python/mod_cvcuda/nvcv/Cache.hpp:57-94](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.hpp#L57-L94)）。它的 `doComputeSizeInBytes` 直接返回 0，注释写明「nvcv 之外的缓存条目（例如 cvcuda 的算子）不应污染缓存」——这呼应 u3-l3 讲过的「算子对象创建也走缓存」，且不占显存配额。

#### 4.3.4 代码实践

**实践目标**：验证包装对象零配额，并对比非包装对象的记账差异。

**操作步骤**：

1. 运行 [samples/object_cache/basic_wrapped.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/basic_wrapped.py#L18-L29)（需要 torch）：它把一个 `device="cuda"` 的 torch 张量用 `as_tensor` 包装。
2. 用如下脚本对比两种对象（示例代码）：

   ```python
   import cvcuda, numpy as np, torch

   cvcuda.clear_cache()
   w = cvcuda.as_tensor(torch.zeros(1024, 1024, 3, device="cuda"), "HWC")
   print("wrapped :", cvcuda.current_cache_size_inbytes())  # 预期 0
   t = cvcuda.Tensor((1024, 1024, 3), np.float32, cvcuda.TensorLayout.HWC)
   print("non-wrap:", cvcuda.current_cache_size_inbytes())  # 预期 > 0（约 12MB，含对齐）
   print("per-item:", cvcuda.internal.nbytes_in_cache(t))
   ```

**需要观察的现象**：包装后计数为 0；自建张量后计数跳增一个 `nbytes_in_cache` 报告的字节数。

**预期结果**：两行输出 `0` 与一个约 12 MB 的数。注意字节数可能略大于 1024×1024×3×4 的理论值（行对齐，见 u2-l1）。待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：既然包装张量不可复用，为什么还要 `Cache::Instance().add(*tensor)` 把它放进缓存？

**答案**：为了保活。算子异步提交在流上，Python 引用可能先于流上的消费消失；缓存持有的 `shared_ptr` 保证对象活到流上的使用真正结束（配合 u4-l1 的 ResourceGuard/GCBag 机制）。所以包装对象「进缓存但不进配额、被查询时先被清理」。

**练习 2**：文档说「缓存复用也适用于包装对象」，这句话和「包装对象不可复用显存」矛盾吗？

**答案**：不矛盾。包装 Image 的路径里，复用的对象是 `nvcvpy::Image` 外壳（含 Resource 基类里的 cudaEvent 等昂贵资源），`setWrapData` 会把新 buffer 的元数据装进旧外壳；显存本身始终归外部框架所有，从不复用。

**练习 3**：`ExternalCacheItem::GetSizeInBytes()` 为什么返回 0？

**答案**：它包装的是 nvcv 之外的条目（典型是 cvcuda 算子句柄）。算子对象占用的是主机侧/驱动侧资源而非大块显存，按 0 记账可以避免算子缓存把显存配额吃光（Cache.hpp L85-90 的注释明确说了「不污染缓存」）。

### 4.4 配额与淘汰：unbounded growth、limit 控制与多线程

#### 4.4.1 概念说明

缓存把「复用」做对了，但也带来新问题：**如果每轮创建的形状都不同，缓存就只进不出**——每个新形状都是新键，旧条目永远不会再被命中，却一直占着配额。官方文档称之为 unbounded cache growth（[docs/sphinx/advanced/object_cache.rst:79-90](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L79-L90)），样例 [samples/object_cache/unbounded_growth.py:25-33](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/unbounded_growth.py#L25-L33) 用随机 h/w 每轮造新形状来演示它。

CV-CUDA 的对策是**按设备设配额，超限整体清空**：

- 默认配额 = 当前设备总显存的**一半**，在 `import cvcuda` 时初始化（[docs/sphinx/advanced/object_cache.rst:100](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L100)）。
- 单个对象大于配额 → 直接不入缓存。
- 加入后总用量超限 → 该设备的**全部**条目被一次性清出（不是 LRU 淘汰）。
- 配额可跨设备叠加设置（CV-CUDA 不追踪数据所在设备，文档因此允许把配额设得比单卡显存还大，见 [docs/sphinx/advanced/object_cache.rst:107-108](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L107-L108)）；设为 0 等效关闭缓存。

多线程语义（[docs/sphinx/advanced/object_cache.rst:120-131](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L120-L131)）一句话概括：**表按线程隔离，账和限额全局共享**。一个线程释放的对象不能被另一个线程复用；而限额与当前用量是所有线程共用的，多线程程序要当心配额被别的线程的缓存吃掉。

#### 4.4.2 核心流程

`Cache::add` 的完整决策（这是本讲最值得背下来的流程）：

```text
Cache::add(item)
  ├─ item.GetSizeInBytes() > 配额?          ── 是 → 直接返回(不入缓存)
  ├─ 当前用量 + item大小 > 配额?
  │    └─ 是 → 把该设备的所有条目 extract 出哈希表
  │            (锁外析构, 避免死锁), 用量归零          ← 整体清空, 非 LRU
  └─ 插入条目, 该设备用量 += item大小
```

用量随时间呈**锯齿形**：稳定形状的循环里曲线是平的（复用，不增长）；形状漂移的循环里曲线爬升到配额，然后瞬间归零，再爬升。

#### 4.4.3 源码精读

淘汰逻辑在 [python/mod_cvcuda/nvcv/Cache.cpp:151-183](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L151-L183)。两个分支分别对应「对象太大不收」与「超限全清」，全清范围**只限当前设备**的条目：

```cpp
        if (item.GetSizeInBytes() > doGetDeviceLimit(dev))
        {
            return;   // 单个对象超配额, 不缓存
        }

        if (item.GetSizeInBytes() + doGetDeviceSize(dev) > doGetDeviceLimit(dev))
        {
            // Evict Only items belonging to this device.
            for (auto it = pimpl->items.begin(); it != pimpl->items.end();)
            {
                if (it->first->deviceId() == dev)
                {
                    savedItems.insert(pimpl->items.extract(it++));
                }
                else
                {
                    ++it;
                }
            }
            Impl::current_size_inbytes[dev] = 0;
        }
```

默认配额的初始化在导出函数里，[python/mod_cvcuda/nvcv/Cache.cpp:411-444](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L411-L444)：遍历每块 GPU，`cache_limit_inbytes[d] = total_mem / 2`。这段代码对无 GPU 环境（CI、CPU-only 构建）做了大量容错——顺带解释了为什么在纯 CPU 机器上 `import cvcuda` 也不会炸。

Python 侧的控制面全部由 `Cache::Export` 导出（[python/mod_cvcuda/nvcv/Cache.cpp:449-505](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L449-L505)）：

| Python API | 作用 |
|-----------|------|
| `cvcuda.clear_cache(scope)` | 清空缓存；`GLOBAL` 清所有线程（先同步并排空 GCBag），`LOCAL` 只清本线程 |
| `cvcuda.cache_size(scope)` | 条目数；`GLOBAL` 累加所有线程实例 |
| `cvcuda.get_cache_limit_inbytes()` / `set_cache_limit_inbytes(n)` | 读/写当前设备配额；设 0 即关闭缓存 |
| `cvcuda.current_cache_size_inbytes()` | 当前设备已占用字节 |
| `cvcuda.internal.nbytes_in_cache(obj)` | 单个缓存对象占的字节数（测试/诊断用） |

`clear_cache` 的第一行 `Stream::SynchronizeAndClearGCBag()`（[python/mod_cvcuda/nvcv/Cache.cpp:453-455](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L453-L455)）是本讲与 u4-l1 的直接接口：清缓存前必须先排空 ResourceGuard 经由辅助流回调持有的资源，否则清理不彻底——注释写明「否则调用者得再提交一个无关算子内存才会释放」。

配额读写的官方行为被 [tests/cvcuda/python/test_cache.py:89-98](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py#L89-L98) 固化：清空后默认配额恰为总显存的一半，且可随意改设。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：亲手证明「每轮变 shape → 缓存不命中 → 无界增长」，并说出后果与解法。

**操作步骤**：

1. 运行官方样例（注意入口处把迭代次数降到 100，[samples/object_cache/unbounded_growth.py:39-40](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/unbounded_growth.py#L39-L40)）：

   ```bash
   python samples/object_cache/unbounded_growth.py
   python samples/object_cache/basic.py
   python samples/object_cache/reuse.py
   ```

   前两个脚本没有打印输出（它们是文档的代码素材），观察计数需要下一步的自写脚本。

2. 写一个带观测的复现脚本（示例代码）：

   ```python
   import random, cvcuda, numpy as np

   cvcuda.clear_cache()
   for i in range(30):
       h, w = random.randint(1000, 2000), random.randint(1000, 2000)
       t = cvcuda.Tensor((h, w, 3), np.float32, cvcuda.TensorLayout.HWC)
       del t   # 形状每轮都新 → 下轮键不同 → fetch 落空 → 新分配
       print(f"{i:2d} items={cvcuda.cache_size()} "
             f"bytes={cvcuda.current_cache_size_inbytes()/2**20:8.1f} MiB "
             f"limit={cvcuda.get_cache_limit_inbytes()/2**20:.0f} MiB")
   ```

3. 再运行 [samples/object_cache/control.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/control.py#L18-L31) 并在你自己的循环前加一行 `cvcuda.set_cache_limit_inbytes(0)` 重跑一次对照。

**需要观察的现象**：

- 无界版本：`items` 与 `bytes` 随迭代单调爬升；若爬到 `limit`，下一次分配后计数**瞬间归零再重新爬升**（锯齿，整体淘汰）。
- 配额设 0 的版本：`items` 恒为 0（对象超限不入缓存），`nvidia-smi` 的进程显存反而随着 `del` 及时回落。

**预期结果与后果说明**：变 shape 循环里缓存永不命中，徒增两重代价——显存被不会再用的旧形状占住，挤占模型与数据预算，最终触发一次性全清；全清又把「本可以复用的热点形状」一并清掉，造成分配风暴与性能抖动。解法按优先级：预分配输出改用 `_into` 变体（u3-l3）、把形状分桶对齐、或调低配额/定期 `clear_cache`。待本地验证（本环境无 GPU）。

#### 4.4.5 小练习与答案

**练习 1**：缓存超限时的淘汰策略是什么？为什么 CV-CUDA 不做 LRU？

**答案**：把当前设备的**全部**条目一次性清出（Cache.cpp L163-178）。不做 LRU 是工程取舍：LRU 需要维护访问顺序、逐条淘汰并精确记账，而这里的典型工作负载是「少数热点形状反复复用、漂移形状一次性的」——到超限那一刻，留下来的大概率都是漂移产物，全清实现最简单且锁持有时间可控（extract 出来的条目在锁外析构）。

**练习 2**：把配额设成 0 会怎样？有什么副作用？

**答案**：任何大小 > 0 的对象都过不了 `item.GetSizeInBytes() > limit` 的第一道检查，直接不入缓存——缓存被关闭，显存随 Python 引用计数即时分配/释放。副作用是失去复用：固定形状的稳态循环退化为每轮 cudaMalloc，性能下降（文档 L110 也这么提醒）。

**练习 3**：两个线程各自跑同形状的循环，A 线程的缓存会把 B 线程的配额吃掉吗？

**答案**：会。缓存实例是 thread_local 的（A 释放的对象 B 复用不了），但 `current_size_inbytes`/`cache_limit_inbytes` 是全局共享的静态表（Cache.cpp L91-93），A 的缓存增长同样消耗共享配额，甚至可能触发把 B 线程缓存里该设备的条目一并清空的超限淘汰。文档 L125-126 的警告说的就是这件事。

## 5. 综合实践

把本讲四条主线串成一个可复现实验：**同一条 flip 管线在三种缓存状态下的行为对比**。

写一个脚本（示例代码，依赖 u1-l2 的环境）：

```python
import time, cvcuda, numpy as np

src = cvcuda.Tensor((1080, 1920, 3), np.uint8, cvcuda.TensorLayout.HWC)

def loop(n=200):
    t0 = time.perf_counter()
    for _ in range(n):
        out = cvcuda.flip(src, 1)   # allocating: 每轮 Tensor::Create → 查缓存
        del out
    return (time.perf_counter() - t0) / n * 1e6  # µs/iter

# 场景 A: 默认配额(半卡), 稳态复用
cvcuda.clear_cache(); a = loop()
# 场景 B: 关闭缓存
cvcuda.set_cache_limit_inbytes(0); cvcuda.clear_cache(); b = loop()
# 场景 C: 恢复配额, 但每轮换 shape 制造不命中
cvcuda.set_cache_limit_inbytes(cvcuda.get_cache_limit_inbytes() or 2**30)
cvcuda.clear_cache(); t0 = time.perf_counter()
for i in range(200):
    h = 1000 + (i % 8)              # 8 种形状轮转, 各自能命中但互不复用
    s = cvcuda.Tensor((h, 1920, 3), np.uint8, cvcuda.TensorLayout.HWC)
    o = cvcuda.flip(s, 1); del o, s
c = (time.perf_counter() - t0) / 200 * 1e6
print(f"A 复用: {a:.1f}µs  B 关缓存: {b:.1f}µs  C 多形状: {c:.1f}µs")
print(f"缓存条目: {cvcuda.cache_size()}, 字节: {cvcuda.current_cache_size_inbytes()/2**20:.1f} MiB")
```

要求完成：

1. 记录三个场景的耗时与最后的 `cache_size`/`current_cache_size_inbytes`，填成表格。
2. 解释场景 A 为何第二轮起近似零分配（对照 4.2 的 fetch→命中路径），场景 B 为何最慢（每轮 cudaMalloc），场景 C 的缓存条目数为何约为形状种数 × 2（输入与输出各一族键）。
3. 用 `_into` 变体改写循环（预分配一个 `dst`，循环内 `cvcuda.flip_into(dst, src, 1)`），再测一次，说明为什么它能彻底绕开本讲讨论的缓存命中问题（提示：u3-l3——dst 由你持有，`Tensor::Create` 这一步根本不发生）。

预期：A < C < B，且 `_into` 版本与 A 相当或更好、行为最确定。待本地验证（本环境无 GPU，无法代跑）。

## 6. 本讲小结

- 对象缓存**只在 Python 绑定层存在**（`python/mod_cvcuda`），核心是每线程一张的 `unordered_multimap`：键 = (规格, 设备)，值 = 持有 `shared_ptr` 的条目。
- 非包装对象**创建即入缓存**：allocating 算子的 `Tensor::Create` 先 `fetch`，键匹配（shape 含 layout + dtype + deviceId）且 `isInUse()` 为假（`use_count() > 2` 的引用计数判据）才命中；因此 `del` 后显存不降，`clear_cache`/超限淘汰/进程退出才真正归还。
- 变长批的键只有 capacity；包装对象零字节记账，进缓存只为流上保活，包装 Image 还会复用昂贵的外壳（Resource 里的 cudaEvent）；算子对象经 `ExternalCacheItem` 入缓存同样记 0 字节。
- 配额按设备记账，默认半张卡；超限策略是**该设备条目整体清空**（非 LRU），单对象超配额直接不收，`set_cache_limit_inbytes(0)` 可关闭缓存。
- 每轮变 shape 的循环是缓存的反面案例：只进不出、无界增长，最终挤占显存并触发全清抖动；解法是 `_into` 变体、形状分桶或主动控制配额。
- 多线程下「表隔离、账共享」：跨线程不复用，但配额与超限淘汰是全局的。

## 7. 下一步学习建议

- 下一讲 u4-l3《多流、多线程与多 GPU》将直接使用本讲的结论：阅读 [tests/cvcuda/python/test_multi_threading.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_threading.py) 时，留意线程局部的缓存如何与每线程的流栈（`StreamStack`）配合。
- 缓存与 ResourceGuard/GCBag 的联动（`clear_cache` 里的 `SynchronizeAndClearGCBag`）在 u8-l3《Workspace 与 per-stream 缓存》会看到 C++ 侧的对应物：`PerStreamCache`、`SimpleCache` 与 Event 回收，那是一套更细粒度的按流缓存。
- 想继续深挖本讲，建议精读 [python/mod_cvcuda/nvcv/Cache.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp) 的析构函数（L103-149，解释器关闭时「故意泄漏」的兜底）与 [tests/cvcuda/python/test_cache.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py)（用 refcount 监测包装对象生命周期的技巧）。

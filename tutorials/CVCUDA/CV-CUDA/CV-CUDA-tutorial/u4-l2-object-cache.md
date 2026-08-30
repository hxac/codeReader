# u4-l2 Python 对象缓存：Tensor/ImageBatch 的自动复用

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出对象缓存（object cache）解决了什么问题：为什么 `cvcuda.Tensor` 被 `del` 后显存没有真正释放。
2. 准确描述缓存的**命中条件**：什么算「规格完全相同」（shape + layout + dtype + 设备 + 对象类型）。
3. 区分 **non-wrapped**（CV-CUDA 自己分配显存）与 **wrapped**（包装外部框架显存）两类对象在缓存中的不同待遇——前者占缓存配额、可复用；后者不占配额、进缓存只为生命周期保护。
4. 解释缓存的**增长规律与失控场景**（unbounded growth）：为什么「每轮换一个 shape」的循环会让缓存永远不命中，以及达到限额时发生的是「整包清空」而非 LRU 淘汰。
5. 用 `cvcuda.cache_size()`、`cvcuda.current_cache_size_inbytes()` 等官方接口做实验，定量验证上述行为。

## 2. 前置知识

本讲是纯 Python 侧机制，不需要 CUDA 编程知识，但需要几个基础概念：

- **引用计数与 Python 的 `del`**：Python 对象靠引用计数管理生命周期。`del x` 只是删掉一个名字（引用），只有当**最后一个**引用消失时对象才被销毁。CV-CUDA 的缓存恰恰会额外持有引用——这是「del 了却没释放显存」的直接原因。
- **C++ 的 `shared_ptr`**：Python 绑定层内部用 `std::shared_ptr<CacheItem>` 管理缓存条目，`use_count()`（引用计数）被用来判断「对象当前是否正在被使用」。
- **`cudaMalloc` 的代价**：GPU 显存分配是昂贵的同步操作（可能伴随设备级锁）。推理/预处理管线通常以每秒几十上百次 的频率创建同形状的中间张量，若每次都真实分配/释放，分配开销会淹没计算本身。
- **哈希表与「键」**：缓存用「键（Key）」判断两个对象是否规格相同。键的等值规则就是命中条件，本讲的核心之一就是读懂这个键。
- **线程局部存储（thread-local）**：每个线程各自拥有一份变量副本。CV-CUDA 的缓存实例是线程局部的，但字节配额是全局共享的——这个不对称是第 5 节的主题。

承接前讲：[u3-l3](u3-l3-allocating-vs-into.md) 已经从外面看到「allocating 变体会查对象缓存」，本讲打开它的内部；[u4-l1](u4-l1-stream-model.md) 讲过的 ResourceGuard（资源守卫）会在 4.3 节再次出现——它正是 wrapped 对象必须进缓存的动机。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [docs/sphinx/advanced/object_cache.rst](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst) | 官方文档：缓存行为、限额、多线程注意事项（本讲的权威大纲） |
| [python/mod_cvcuda/nvcv/Cache.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp) | 缓存本体：`unordered_multimap` 存储、add/fetch/清空、限额记账、导出全部 Python API |
| [python/mod_cvcuda/nvcv/Cache.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.hpp) | `CacheItem`/`ExternalCacheItem` 接口：条目如何报告自己的字节大小 |
| [python/mod_cvcuda/include/nvcv/python/Cache.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/Cache.hpp) | `IKey` 基类：设备号捕获、hash 与相等判定的公共框架 |
| [python/mod_cvcuda/nvcv/Tensor.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp) | Tensor 的两条创建路径（真分配 vs 包装）与 `Tensor::Key` 的命中规则 |
| [python/mod_cvcuda/nvcv/ImageBatch.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageBatch.cpp) | `ImageBatchVarShape` 以 capacity 为键的缓存复用 |
| [python/mod_cvcuda/operators/OpFlip.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp) | allocating 变体如何借道 `Tensor::Create` 触发缓存 |
| [samples/object_cache/](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/basic.py) | 官方实验样例：basic / basic_wrapped / reuse / unbounded_growth / control / threads |
| [tests/cvcuda/python/test_cache.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py) | 官方测试：把缓存行为固化成断言，是「预期结果」的金标准 |

## 4. 核心概念与源码讲解

### 4.1 为什么需要对象缓存：Python 管线中的重复分配问题

#### 4.1.1 概念说明

一条典型的 CV-CUDA 推理预处理管线（解码 → resize → cvtcolor → normalize）里，**中间张量的生命周期极短**：每帧创建、用完即弃。若按朴素方式实现，每个中间张量都要经历一次 `cudaMalloc` 和一次 `cudaFree`。

更糟的是 Python 的语义陷阱：用户即使写了 `del tensor`，以为释放了显存，`nvidia-smi` 里的占用却纹丝不动。官方文档把这件事讲得很直白：**由 CV-CUDA 分配的 `Tensor`/`Image` 离开作用域后，底层显存不会被释放，而是存进缓存等待复用**；因此「不要试图手动释放内存」是最佳实践。

所以这个缓存同时回答了两个问题：

1. **性能**：同规格对象反复创建时跳过真实分配，稳态零 `cudaMalloc`。
2. **语义**：解释「del 了为什么不掉显存」，并给出手动兜底手段 `cvcuda.clear_cache()`。

还有一条重要的边界：**只有 Python 对象有缓存，C/C++ 层没有**。用 C++ API 创建的 `nvcv::Tensor` 离开作用域就真实析构。这个缓存是 `python/mod_cvcuda` 绑定层的设施，与 `src/cvcuda` 核心库无关。

#### 4.1.2 核心流程

```text
Python: cvcuda.Tensor(shape, dtype, layout)
   │
   ├─ 计算键 Key = (TensorShape[shape+layout], dtype, deviceId)
   ├─ Cache.fetch(Key)
   │     ├─ 命中（存在规格相同且不在使用的条目）→ 直接返回缓存对象，零分配
   │     └─ 未命中 → new Tensor（内部 cudaMalloc）→ Cache.add() 入缓存 → 返回
   │
Python: del tensor
   │
   └─ 只删引用；缓存仍持有一个引用 → 显存保留，等下次同规格创建复用
```

#### 4.1.3 源码精读

官方文档开头就划定了缓存管辖的对象类型与「仅 Python」的边界：

[docs/sphinx/advanced/object_cache.rst:L22-L30](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L22-L30)——这段说明 `cvcuda.Image`、`cvcuda.Tensor`、`cvcuda.ImageBatchVarShape`、`cvcuda.TensorBatch` 都由缓存自动管理，并明确「只有 Python 对象被缓存，没有 C/C++ 对象缓存」「CV-CUDA 不追踪数据所在设备」（后者是 4.5 节多 GPU 行为的伏笔）。

关于 `del` 的语义：

[docs/sphinx/advanced/object_cache.rst:L50-L62](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L50-L62)——`del` 只移除引用；当缓存成为唯一持有者时，底层显存即可被后续创建复用；可用 `cvcuda.clear_cache()` 手动清空。

缓存实例本身是**每个线程一个**：

[python/mod_cvcuda/nvcv/Cache.cpp:L376-L380](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L376-L380)——`Cache::Instance()` 返回 `thread_local Cache cache;`，即每线程一份独立的哈希表；同时每个实例构造时把自己登记进静态集合 `instances`（见 [python/mod_cvcuda/nvcv/Cache.cpp:L96-L101](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L96-L101)），供 `ClearAll()`/`TotalSize()` 跨线程汇总。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「del 不释放、clear_cache 才释放」。

**操作步骤**（示例代码，基于官方样例改写）：

```python
# 示例代码：cache_probe.py
import cvcuda, numpy as np

def mk():
    return cvcuda.Tensor((16, 32, 4), np.float32, cvcuda.TensorLayout.HWC)

t = mk()
print("创建后条目数:", cvcuda.cache_size(), "字节数:", cvcuda.current_cache_size_inbytes())
del t
print("del 后  条目数:", cvcuda.cache_size(), "字节数:", cvcuda.current_cache_size_inbytes())
cvcuda.clear_cache()
print("clear 后条目数:", cvcuda.cache_size(), "字节数:", cvcuda.current_cache_size_inbytes())
```

**需要观察的现象**：`del` 前后 `current_cache_size_inbytes()` 不变（条目仍在缓存里）；`clear_cache()` 之后归零。

**预期结果**：三行输出形如 `1 / N字节 → 1 / N字节 → 0 / 0字节`。注意：即使 `del` 之后条目仍在缓存中（`cache_size()` 至少为 1），这也与官方 `test_cache_limit_clearing` 中「先建后删仍按缓存记账」的行为一致（[tests/cvcuda/python/test_cache.py:L146-L155](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py#L146-L155)）。具体数值**待本地验证**（本讲义写作环境无 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：为什么说「在 CV-CUDA 管线里不要写 `del tensor` 试图省显存」？

**答案**：`del` 只减少 Python 引用；只要缓存还持有引用（且配额未触发清空），显存就不会归还驱动。手动 `del` 既达不到释放目的，还剥夺了缓存复用的机会——下一次同规格创建本可以直接命中。真正需要强制释放时用 `cvcuda.clear_cache()`。

**练习 2**：C++ 程序里 `nvcv::Tensor` 析构时显存会立即释放吗？

**答案**：会。对象缓存只存在于 `python/mod_cvcuda` 绑定层（`Cache::Instance()` 只被 Python 侧代码调用），C++/C API 创建的张量没有缓存，`shared_ptr` 引用归零即析构、释放显存。

### 4.2 缓存核心数据结构与命中条件：multimap + IKey

#### 4.2.1 概念说明

缓存要回答一个核心问题：**「新对象」和「旧对象」什么时候算同一个规格？** 这由三层判定共同决定：

1. **对象的具体 Key 类型**：Tensor 的键不会和 Image 的键相等（C++ `typeid` 先比一轮）。
2. **CUDA 设备号**：键在构造时用 `cudaGetDevice` 抓取当前设备；不同 GPU 上的条目互不命中。
3. **规格本体**：对 Tensor 而言是 `TensorShape`（shape **连同 layout 标签**，见 u2-l1）+ `dtype`。

存储上用的是 `unordered_multimap`（一键多值）而非 `map`（一键一值）：同一个规格可能同时存在多个空闲条目——比如你同时持有 3 个同 shape 的张量再全部释放，缓存里就有 3 个可复用条目。

#### 4.2.2 核心流程

```text
add(item):
  若 item字节 > 设备限额        → 直接不入缓存（return）
  若 已用字节 + item字节 > 限额  → 把该设备的所有条目整包移出（记账清零）
  插入 multimap[key] = item；记账 += item字节

fetch(key):
  在 multimap 里找所有键等于 key 的条目
  过滤掉「正在被使用」的（isInUse）
  返回剩余的空闲条目列表（调用方取第一个）

isInUse(): shared_ptr 引用计数 > 2
  （>2 意味着除「缓存持有」和「本次临时持有」之外还有别人在用）
```

命中条件的形式化表述：

\[
\text{hit}(k_{new}, k_{old}) \iff \text{typeid}(k_{new}) = \text{typeid}(k_{old}) \;\wedge\; dev_{new} = dev_{old} \;\wedge\; (\text{shape}, \text{layout}, \text{dtype})_{new} = (\text{shape}, \text{layout}, \text{dtype})_{old}
\]

#### 4.2.3 源码精读

存储结构：

[python/mod_cvcuda/nvcv/Cache.cpp:L86-L94](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L86-L94)——`Items` 是以 `const IKey *` 为键的 `unordered_multimap`，值是 `shared_ptr<CacheItem>`；哈希与相等由 `HashKey`/`KeyEqual` 代理给 `IKey::hash()`/`operator==`。特别注意 `cache_limit_inbytes` 和 `current_size_inbytes` 是 `inline static`——**哈希表线程私有，但字节记账全局共享**（4.5 节展开）。

判定「是否正在被使用」：

[python/mod_cvcuda/nvcv/Cache.cpp:L78-L84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L78-L84)——`isInUse()` 检查 `use_count() > 2`：缓存里的 `shared_ptr` 占 1 个计数，`shared_from_this()` 的临时拷贝占 1 个，再多就说明外界（Python 变量、正在执行的算子等）还引用着它。这个「魔法数字 2」的写法依赖当前持有结构，是读源码时值得停下来想一想的点。

键的公共框架：

[python/mod_cvcuda/include/nvcv/python/Cache.hpp:L31-L71](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/Cache.hpp#L31-L71)——`IKey` 构造函数里 `cudaGetDevice(&m_deviceId)` 抓取设备号；`hash()` 把派生类哈希、`typeid` 哈希和设备号哈希混在一起；`operator==` 依次比较 `typeid`、设备号，最后才调派生类的 `doIsCompatible`。**这三层就是命中条件的全部来源。**

Tensor 的规格判定：

[python/mod_cvcuda/nvcv/Tensor.cpp:L345-L364](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L345-L364)——`Tensor::Key::doIsCompatible` 在两个键都不是 wrapper 时，比较 `std::tie(m_shape, m_dtype)` 是否相等。`m_shape` 是 `nvcv::TensorShape`（shape + layout），所以 `(16,32,4)+HWC` 与 `(16,32,4)+CHW` **不互相命中**，`(16,32,4)+HWC` 与 `(1,16,32,4)+NHWC` 也**不互相命中**（rank 不同）。wrapper 分支（`m_wrapper`）的行为在 4.3 节专门讲。

创建路径上的 fetch-or-create：

[python/mod_cvcuda/nvcv/Tensor.cpp:L85-L103](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L85-L103)——`Tensor::CreateFromReqs` 先用 `Key{reqs}` 查缓存；命中则 `static_pointer_cast` 后直接返回（仅留一个断言确认 dtype 一致）；未命中才 `new Tensor(reqs)` 并 `Cache::Instance().add(*tensor)`。所有 Python 侧张量创建（包括算子 allocating 变体的隐式输出分配）都汇聚到这个函数。

其他容器的键略有不同，但套路一致：

- [python/mod_cvcuda/nvcv/ImageBatch.cpp:L42-L73](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ImageBatch.cpp#L42-L73)——`ImageBatchVarShape` 的键**只有 capacity**（不关心 maxsize/格式），命中后先 `batch->clear()` 复位成崭新状态再返回；`TensorBatch` 同样以 capacity 为键（[python/mod_cvcuda/nvcv/TensorBatch.cpp:L57-L63](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/TensorBatch.cpp#L57-L63)）。这与 u2-l3 讲过的「变长批以 capacity 为缓存键」呼应。
- 算子对象也走同一套缓存：[python/mod_cvcuda/operators/Operators.hpp:L195-L224](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/Operators.hpp#L195-L224)——`CreateOperatorEx` 的模板逻辑与 `CreateFromReqs` 一模一样：fetch 为空则构造并 add，否则复用。这是 u3-l3 说「算子对象本身也走缓存」的出处。

#### 4.2.4 代码实践

**实践目标**：验证命中条件中「layout 参与键」。

**操作步骤**（示例代码）：

```python
# 示例代码：key_probe.py
import cvcuda, numpy as np

def report(tag):
    print(tag, "条目数:", cvcuda.cache_size(), "字节数:", cvcuda.current_cache_size_inbytes())

cvcuda.clear_cache()
report("清空后")

a = cvcuda.Tensor((16, 32, 4), np.float32, cvcuda.TensorLayout.HWC)
report("建 HWC (16,32,4)")
del a

b = cvcuda.Tensor((16, 32, 4), np.float32, cvcuda.TensorLayout.CHW)  # 同数值 shape，不同 layout
report("建 CHW (16,32,4)")
del b

c = cvcuda.Tensor((16, 32, 4), np.float32, cvcuda.TensorLayout.HWC)  # 与 a 完全同规格
report("再建 HWC (16,32,4)")
del c
```

**需要观察的现象**：第二次（CHW）创建后条目数/字节数**继续增长**（没有命中 HWC 的条目）；第三次（HWC）创建后**不再增长**（命中了第一次的空闲条目）。

**预期结果**：四次输出形如 `0 → 1 → 2 → 2`（条目数）。精确数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么缓存用 `unordered_multimap` 而不是 `unordered_map`？

**答案**：同一规格可能有多个条目同时存在：多个同规格对象被同时持有（`isInUse` 为真）时，新创建的会作为**同键的新条目**插入；它们后来被释放就成了多个空闲条目。`unordered_map` 一键一值会强迫立即淘汰，无法表达这种「同规格多条目」的状态。

**练习 2**：`isInUse()` 为什么是 `use_count() > 2` 而不是 `> 1`？

**答案**：`isInUse()` 本身通过 `shared_from_this()` 取共享指针，这个临时对象本身就占 1 个计数；缓存表里还存着 1 个计数。所以「只有缓存和自己」时计数恰为 2，超过 2 才说明有外部持有者（Python 变量、正在执行的算子等）。

**练习 3**：`cvcuda.Tensor(2, (37, 7), cvcuda.Format.RGB8, rowalign=1)` 与 `cvcuda.Tensor(2, (37, 7), cvcuda.Format.RGB8)`（默认 rowalign）会互相命中吗？

**答案**：不会。`rowalign` 影响行对齐，进而进入 `Tensor::Requirements`，最终体现在不同的 `TensorShape`/stride 上，键不同则不命中。想复用就必须保持包括对齐参数在内的整套规格一致。

### 4.3 non-wrapped 与 wrapped：两条进入缓存的路径

#### 4.3.1 概念说明

按**显存所有权**划分，对象分两类（官方文档的原始区分）：

| | non-wrapped（非包装） | wrapped（包装） |
|---|---|---|
| 显存由谁分配 | CV-CUDA（`cudaMalloc`） | 外部框架（torch/cupy/numpy 经 DLPack/CAI） |
| 典型来源 | `cvcuda.Tensor(shape, dtype, layout)`、算子 allocating 输出 | `cvcuda.as_tensor(torch_tensor, layout)` |
| 占用缓存字节配额 | 占（按真实字节数记账） | **不占**（按 0 记账） |
| 能否被复用 | 能（同规格创建直接拿旧显存） | 不能复用（显存属于外部，包装外壳用完即弃） |
| 进缓存的目的 | 复用 | **生命周期保护** |

wrapped 对象「也进缓存却不算尺寸」乍看矛盾，其实目的完全不同：算子是**异步提交到流上**的（u4-l1），Python 侧函数返回时 kernel 可能还没执行。如果包装张量在 Python 里已经没人引用，而 C++ 侧又只被流回调短暂持有，对象可能在 kernel 执行期间被析构——外部框架把显存一回收，kernel 就写进了野指针。把 wrapped 对象放进缓存，等于让缓存多持有一个引用，撑到流上的工作完成。Tensor.cpp 里的注释原话是：「不这么做，事情可能会坏掉」。

#### 4.3.2 核心流程

```text
non-wrapped: Tensor::Create → CreateFromReqs
    fetch(Key{shape+layout, dtype}) ── 命中 → 返回旧对象（显存复用）
                                    └─ 未命中 → new + cudaMalloc → add（按真实字节记账）

wrapped: Tensor::Wrap(ExternalBuffer)
    用「wrapper 专用键」removeAllNotInUseMatching  ← 先清掉旧的不在用的包装外壳
    new Tensor(外部数据, 外部对象引用)               ← 不分配显存
    add 进缓存                                      ← 记账按 0 字节，只为保命
```

#### 4.3.3 源码精读

官方文档对两类的定义（配合官方样例）：

[docs/sphinx/advanced/object_cache.rst:L31-L48](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L31-L48)——non-wrapped 由 CV-CUDA 分配并**增加缓存占用**（引用 [samples/object_cache/basic.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/basic.py#L24-L27)：直接 `cvcuda.Tensor((16, 32, 4), np.float32, cvcuda.TensorLayout.HWC)`）；wrapped 包装外部管理的显存，**不增加缓存占用**（引用 [samples/object_cache/basic_wrapped.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/basic_wrapped.py#L24-L26)：`cvcuda.as_tensor(torch_tensor, layout="N")`）。

包装路径的完整逻辑：

[python/mod_cvcuda/nvcv/Tensor.cpp:L168-L203](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L168-L203)——`Tensor::Wrap` 三步走：① 用 wrapper 键调 `removeAllNotInUseMatching` 清理旧的不在使用的包装外壳（注释说明它们「反正不可复用」）；② 构造包装张量，并用 `buffer.producerStream()` 给资源状态播种生产者流（这正是 u4-l1 讲过的跨流安全机制）；③ `Cache::Instance().add(*tensor)`——注释写明加进缓存是为了「防止它们被 cuda stream 在最后一次使用时销毁，而 python 脚本又不持有引用」。

「不占配额」的字节来源：

[python/mod_cvcuda/nvcv/Tensor.cpp:L246-L251](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L246-L251)——包装张量的构造函数用一份**空的** `nvcv::Tensor::Requirements()` 计算 `m_size_inbytes`（对照非包装构造函数 [L239-L244](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L239-L244)，后者用真实 `reqs` 计算）。空的内存需求算出的字节数为 0，于是 `add()` 里的记账 `+= 0`，包装对象对配额零影响。

wrapper 键的「一视同仁」：

[python/mod_cvcuda/nvcv/Tensor.cpp:L332-L343](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L332-L343)——wrapper 键的哈希恒为 0（注释：「对缓存而言所有 wrapper 都相等」）。这配合 `doIsCompatible` 的 wrapper 分支（[L345-L359](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L345-L359)：两个 wrapper 键互相兼容、wrapper 与非 wrapper 永不兼容），使 `removeAllNotInUseMatching` 一次就能捞出**所有**空闲的包装外壳。

算子对象作为「外部缓存条目」也不占配额：

[python/mod_cvcuda/nvcv/Cache.hpp:L57-L94](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.hpp#L57-L94)——`ExternalCacheItem::doComputeSizeInBytes()` 直接 `return 0;`，注释写明「外部 CacheItem（例如 cvcuda 的算子）不应污染缓存」。这解释了为什么每帧 `cvcuda.flip(...)` 内部 `CreateOperator` 反复查缓存，`current_cache_size_inbytes()` 却不增长。

官方测试对此有直接断言：

[tests/cvcuda/python/test_cache.py:L128-L143](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py#L128-L143)——先用 cupy 数组 `as_tensor` 包装：断言 `current_cache_size_inbytes() == 0`（包装不占）；再调用 allocating 算子 `advcvtcolor` 产生非包装输出：断言字节数等于该输出的缓存字节数且 `> 0`。

#### 4.3.4 代码实践

**实践目标**：对比两类对象的缓存记账差异。

**操作步骤**（示例代码）：

```python
# 示例代码：wrapped_probe.py（需要 torch；无 torch 可换成 cupy）
import cvcuda, numpy as np, torch

cvcuda.clear_cache()
print("清空后:", cvcuda.current_cache_size_inbytes())

ext = torch.zeros((16, 32, 4), device="cuda", dtype=torch.float32)
wrapped = cvcuda.as_tensor(ext, "HWC")
print("包装后:", cvcuda.current_cache_size_inbytes())   # 预期仍为 0

own = cvcuda.Tensor((16, 32, 4), np.float32, cvcuda.TensorLayout.HWC)
print("自建后:", cvcuda.current_cache_size_inbytes())   # 预期 > 0
```

**需要观察的现象**：包装 torch 张量前后字节数都是 0；自建同规格张量后字节数跳增。

**预期结果**：三行输出 `0 → 0 → N（N≈16×32×4×4 字节，可能因行对齐略大）`。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：既然 wrapped 对象不可复用，为什么还要 `Cache::Instance().add(*tensor)`？

**答案**：为了生命周期保护。算子异步提交到流，Python 返回时 kernel 可能尚未执行完；若 Python 侧已无引用，包装张量可能被析构、外部框架回收显存，导致 kernel 写野指针。缓存持有的那个 `shared_ptr` 让对象活到流上工作结束（配合 u4-l1 的资源守卫记账）。

**练习 2**：`Tensor::Wrap` 为什么在构造新包装对象**之前**先调 `removeAllNotInUseMatching`？

**答案**：wrapper 键哈希恒 0、互相兼容，这次调用会把所有「不在使用」的旧包装外壳一次性清出缓存——它们反正不能复用，留着只占条目数。这是「创建时顺手做垃圾回收」的模式。

### 4.4 缓存增长控制：限额、整包清空与 unbounded growth

#### 4.4.1 概念说明

缓存的键是「规格」。如果每轮循环的输出规格都不同（视频分辨率变化、动态 batch、随机尺寸数据增强……），每一轮都会 miss 并新增一个条目，缓存条目数随时间**无界增长**——官方文档称之为 unbounded growth 风险。更隐蔽的是：这些显存全部被缓存持有着，其他框架（比如同进程的 torch）会率先看到 OOM。

CV-CUDA 的对策是一个**按设备的字节限额**加一条简单粗暴的规则：

- 默认限额 = 当前设备总显存的 **一半**（`import cvcuda` 时逐卡初始化）。
- 单个对象超过限额 → 根本不入缓存（每次都真实分配/释放）。
- 加入后累计将超过限额 → **清空该设备的全部缓存条目**（不是 LRU 淘汰最旧的，而是整包倒掉），再插入新条目。

「整包清空」意味着缓存尺寸呈**锯齿形**：爬升到限额 → 一次性跌回单个条目的大小 → 再爬升。理解这一点对解释基准测试里的偶发耗时尖峰很有价值。

#### 4.4.2 核心流程

```text
import cvcuda 瞬间：
  for 每块 GPU d: cache_limit_inbytes[d] = total_mem(d) / 2

add(item) 时（设备 d）：
  size(item) > limit(d)               → 不缓存，直接返回
  used(d) + size(item) > limit(d)     → 清空设备 d 的所有条目（used(d) 归 0）
                                       → 插入 item，used(d) = size(item)
  否则                                → 插入 item，used(d) += size(item)

set_cache_limit_inbytes(n)：
  n < 0           → 抛 invalid_argument
  n > 总显存       → 打印 WARNING（但允许，因为 CV-CUDA 不追踪设备）
  used(d) > n     → 先清空设备 d 的缓存再生效
```

#### 4.4.3 源码精读

add 的完整限额逻辑：

[python/mod_cvcuda/nvcv/Cache.cpp:L151-L183](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L151-L183)——第一处判断（L158-L161）：对象本身大于限额就直接 `return` 不缓存；第二处（L163-L178）：累计将超限时，遍历并 `extract` 出**该设备**的全部条目（注释明确「只淘汰属于这个设备的条目」，多卡互不干扰），把记账归零；最后 `emplace` 插入并累加字节。注意清出的条目被移进局部容器 `savedItems`，在锁外析构——与 `removeAllNotInUseMatching` 的注释（[L185-L197](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L185-L197)）一样，是为了避免析构递归回调缓存导致死锁。

默认限额的初始化：

[python/mod_cvcuda/nvcv/Cache.cpp:L411-L444](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L411-L444)——`Export()` 里逐卡 `cudaMemGetInfo` 后设 `cache_limit_inbytes[d] = total_mem / 2`；前面一大段注释解释了为何要容忍无 GPU 主机（stub libcuda、`CUDA_VISIBLE_DEVICES=""` 等）：此时跳过初始化，`import cvcuda` 仍可用，等真有设备时再计。

修改限额：

[python/mod_cvcuda/nvcv/Cache.cpp:L286-L327](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L286-L327)——`setCacheLimit` 负数抛异常；超过当前卡总显存打印 WARNING 但仍接受（官方文档举的例子：两块 24GB 卡可以把限额设到 40GB 以上，因为 CV-CUDA 不追踪数据在哪个设备）；若当前已用字节超过新限额，先整包清空该设备。

unbounded growth 的官方复现脚本：

[samples/object_cache/unbounded_growth.py:L25-L33](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/unbounded_growth.py#L25-L33)——循环里 `random.randint(1000, 2000)` 取 h 和 w，每轮创建 `(h, w, 3)` float32 张量。随机组合几乎不重复，键几乎不重合，**永远 miss**：每轮真实分配一块 12~48MB 的显存并滞留缓存，直到撑爆限额触发整包清空。文档对应说明在 [docs/sphinx/advanced/object_cache.rst:L79-L90](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L79-L90)。

官方测试固化的三条规则：

[tests/cvcuda/python/test_cache.py:L146-L170](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py#L146-L170)——① 把限额设到小于当前缓存尺寸 → 缓存立即清空；② 对象尺寸超过限额 → 不入缓存（字节数保持 0）；③ 限额恰等于一个对象尺寸时，连续创建两个同规格对象（第一个还被 Python 引用着、不能复用）→ 触发整包清空后插入第二个，字节数仍等于单对象尺寸。这三条断言就是 4.4.2 伪代码的可执行版本。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：复现「不命中」并量化后果；再运行官方 basic/reuse 样例观察命中路径。

**操作步骤**：

1. 运行官方复用样例（它本身无输出，配合探针观察）：

```bash
python samples/object_cache/reuse.py
```

2. 给它加打印（示例代码，保存为 `reuse_probe.py`）：

```python
# 示例代码：reuse_probe.py
import cvcuda, numpy as np

def create_tensor1():
    tensor1 = cvcuda.Tensor((16, 32, 4), np.float32, cvcuda.TensorLayout.HWC)
    print("tensor1 存活中:", cvcuda.cache_size(), cvcuda.current_cache_size_inbytes())

def create_tensor2():
    tensor2 = cvcuda.Tensor((16, 32, 4), np.float32, cvcuda.TensorLayout.HWC)
    print("tensor2 复用时:", cvcuda.cache_size(), cvcuda.current_cache_size_inbytes())

cvcuda.clear_cache()
create_tensor1()   # tensor1 离开作用域后被缓存持有
create_tensor2()   # 同规格 → 命中 tensor1 的显存，零新分配
```

3. 写「故意不命中」的循环（示例代码）：

```python
# 示例代码：miss_probe.py
import cvcuda, numpy as np

cvcuda.clear_cache()
for i in range(5):
    _ = cvcuda.Tensor((100 + i, 200 + i, 3), np.float32, cvcuda.TensorLayout.HWC)
    print(f"第 {i} 轮: 条目数={cvcuda.cache_size()}, 字节数={cvcuda.current_cache_size_inbytes()}")
```

4. 运行三个脚本：`python samples/object_cache/basic.py`、`python reuse_probe.py`、`python miss_probe.py`（basic.py 无打印，仅为确认可运行）。

**需要观察的现象**：

- `reuse_probe.py`：两次打印的**字节数相同**——`tensor2` 没有带来新分配（复用了 `tensor1` 留下的显存）；这正是官方 `reuse.py`（[samples/object_cache/reuse.py:L23-L40](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/reuse.py#L23-L40)）演示的「同规格创建不发生新内存分配」。
- `miss_probe.py`：每轮条目数 +1、字节数单调上涨——五轮五个不同的键，无一命中。

**预期结果**：`miss_probe.py` 输出形如 `1 → 2 → 3 → 4 → 5`（条目数）。把循环次数加大（并放大尺寸），字节数爬到限额一半时会瞬间跌回单条目大小（整包清空的锯齿）。具体数值**待本地验证**。

**后果说明**（实践任务要求的分析）：不命中循环的后果有三层——① 每轮真实 `cudaMalloc`，分配开销无法摊销，吞吐下降；② 旧显存全部滞留缓存，**其他框架可见的空闲显存被蚕食**，同进程的 torch/cupy 可能先 OOM；③ 达到限额时的整包清空会连带你**正在依赖的**其他规格的缓存条目一起被清掉（比如管线上一步刚建立的可复用输出），造成偶发的性能塌陷。

#### 4.4.5 小练习与答案

**练习 1**：把限额设为 0 会怎样？

**答案**：等于禁用缓存——任何对象都不满足「尺寸 ≤ 0」的入缓存条件，每次创建都真实分配、释放即归还。功能正确但性能受损（官方文档 [L110](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/advanced/object_cache.rst#L110) 明确此点）。

**练习 2**：为什么「超限清空」选择整包倒掉而不是 LRU？

**答案**：从源码看这是工程取舍：条目存在 `unordered_multimap` 里没有时间序，精确 LRU 需要额外的年龄跟踪与锁开销；而缓存的典型稳态是「少数几个规格反复复用、总量远低于限额」，整包清空代价低且实现一行 `extract` 循环即可。代价是清空是全局性的：命中模式多样的进程会周期性损失全部热条目（锯齿形性能）。

**练习 3**：一个 16GB 显存的进程里跑了「随机分辨率增强」循环，另一个跑了「固定 1080p 管线」，谁的 `current_cache_size_inbytes()` 曲线更平稳？

**答案**：固定分辨率管线。它只有少数几个规格，首轮 miss 后持续命中，曲线是水平线；随机分辨率的曲线是锯齿——持续爬升到 8GB（默认限额）后整包清空、再爬升。

### 4.5 线程语义与 Python API 全景

#### 4.5.1 概念说明

缓存实例是**线程局部**的：A 线程释放的对象，B 线程不能复用（文档原话）。但字节记账与限额是所有线程**共享**的静态变量——所以多线程程序里，每个线程各自往共享的配额里存钱。这带来两条实践准则：

1. 多线程程序中警惕「限额被别人改小/清空」的相互影响（文档的 warning）。
2. `clear_cache`/`cache_size` 都接受 `ThreadScope.GLOBAL`（默认，作用于所有线程）或 `ThreadScope.LOCAL`（只作用于当前线程）。

最后把全部 Python API 汇总成一张速查表：

| API | 作用 |
|---|---|
| `cvcuda.cache_size(scope=GLOBAL)` | 缓存条目数量（GLOBAL=所有线程求和，LOCAL=本线程） |
| `cvcuda.current_cache_size_inbytes()` | 当前设备已缓存的字节数 |
| `cvcuda.get_cache_limit_inbytes()` / `set_cache_limit_inbytes(n)` | 读/写当前设备的字节限额 |
| `cvcuda.clear_cache(scope=GLOBAL)` | 清空缓存（先排空流回调） |
| `cvcuda.internal.nbytes_in_cache(item)` | 查询单个缓存条目占用的字节数（测试/调试用） |

#### 4.5.2 核心流程

```text
cache_size(GLOBAL): 对 instances 里每个线程实例求 size() 之和（持全局锁）
cache_size(LOCAL):  只数当前线程实例的条目
clear_cache(GLOBAL): Stream::SynchronizeAndClearGCBag() → 合并所有实例的条目并丢弃，记账清零
clear_cache(LOCAL):  同上，但只清当前线程实例
脚本退出时:         RegisterCleanup 注册的 Cache::ClearAll() 兜底清空
```

#### 4.5.3 源码精读

Python API 的导出与 clear 的顺序细节：

[python/mod_cvcuda/nvcv/Cache.cpp:L446-L471](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L446-L471)——先 `util::RegisterCleanup(m, Cache::ClearAll)` 保证脚本结束时清空；`clear_cache` 的 lambda 第一行是 `Stream::SynchronizeAndClearGCBag()`，注释点明原因：ResourceGuard 通过辅助流回调释放已完成的持有，必须先排空这些回调，清缓存才能真正释放资源（与 u4-l1 的流记账闭环衔接）。随后按 scope 分派 `ClearAll()`（全局）或 `Instance().clear()`（本线程）。

查询类 API：

[python/mod_cvcuda/nvcv/Cache.cpp:L473-L508](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L473-L508)——`cache_size` 按 scope 返回 `TotalSize()`（对 `instances` 求和）或 `Instance().size()`；`get/set_cache_limit_inbytes` 与 `current_cache_size_inbytes` 都以 `cudaGetDevice` 取**当前设备**为口径；`internal.nbytes_in_cache` 暴露单条目字节数给测试用。

官方多线程样例（浓缩了全部语义）：

[samples/object_cache/threads.py:L25-L38](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/threads.py#L25-L38)——主线程建 1 个张量后启动子线程；子线程内再建 1 个张量时打印 `(2, 1)`：GLOBAL=2（两个线程各 1 条），LOCAL=1（只数本线程）；`clear_cache(LOCAL)` 后打印 `(1, 0)`：只清掉了子线程自己的条目。

该样例后半段的长注释也值得读：

[samples/object_cache/threads.py:L42-L65](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/threads.py#L42-L65)——解释了 `thread.join()` 与 C++ thread_local 析构之间的竞态：Python 认为线程已结束时，C++ 侧线程局部对象（包括 Cache 实例）还在异步析构，主线程若随即退出，两个 Cache 析构函数会并发清理 CUDA 资源导致段错误；样例用 `time.sleep(0.1)` 规避。这是「Python 线程模型与 C++ 线程局部存储交错」的鲜活教材，正常业务（长寿命线程池）不会踩到。

对应地，`Cache` 析构函数里有一整套防御：

[python/mod_cvcuda/nvcv/Cache.cpp:L103-L148](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L103-L148)——先在锁内手动扣减记账（注释：「在这里调析构函数可能不安全」），再视 Python 解释器状态决定是否持 GIL 销毁条目；异常路径干脆故意泄漏（注释链接到 pybind11 关于 GIL 错误的文档），也不冒段错误风险。

#### 4.5.4 代码实践

**实践目标**：验证「实例线程私有、记账全局共享」。

**操作步骤**（示例代码）：

```python
# 示例代码：thread_probe.py
import threading, cvcuda, numpy as np

def worker():
    t = cvcuda.Tensor((16, 32, 4), np.float32, cvcuda.TensorLayout.HWC)
    print("子线程内: GLOBAL=%d LOCAL=%d" %
          (cvcuda.cache_size(), cvcuda.cache_size(cvcuda.ThreadScope.LOCAL)))
    cvcuda.clear_cache(cvcuda.ThreadScope.LOCAL)
    print("LOCAL 清空后: GLOBAL=%d LOCAL=%d" %
          (cvcuda.cache_size(), cvcuda.cache_size(cvcuda.ThreadScope.LOCAL)))

cvcuda.clear_cache()
main_t = cvcuda.Tensor((16, 32, 4), np.float32, cvcuda.TensorLayout.HWC)
th = threading.Thread(target=worker)
th.start(); th.join()
```

**需要观察的现象**：子线程内 GLOBAL 数到主线程+子线程两个条目，LOCAL 只数到 1；`clear_cache(LOCAL)` 后 GLOBAL 剩 1（主线程的还在）、LOCAL 归 0。

**预期结果**：两行输出 `(2, 1)` 和 `(1, 0)`——与官方 `threads.py` 注释里的预期输出完全一致（[samples/object_cache/threads.py:L29-L31](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/object_cache/threads.py#L29-L31)）。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：4 个线程各自缓存了 100 个条目，`cvcuda.cache_size()` 与 `cvcuda.cache_size(cvcuda.ThreadScope.LOCAL)` 分别返回什么？

**答案**：GLOBAL 返回约 400（各线程实例求和；若主线程也有缓存则更多），LOCAL 返回当前调用线程自己的 100。

**练习 2**：为什么 `clear_cache` 要先调 `Stream::SynchronizeAndClearGCBag()`？

**答案**：ResourceGuard 的跨流保护靠辅助流上的回调在 kernel 完成后释放持有（u4-l1）；若不先排空这些回调就清缓存，回调持有的引用还在，条目要么清不掉、要么在回调触发时命中已失效的状态。先同步、后清理，才能保证引用链归零。

**练习 3**：`set_cache_limit_inbytes` 是按进程还是按设备生效？

**答案**：按设备。函数内部用 `cudaGetDevice` 取当前设备，只写 `cache_limit_inbytes[当前设备]`；清空判断也只淘汰该设备的条目（官方多卡测试 `test_per_device_cache_limits` 固化了这一行为，[tests/cvcuda/python/test_cache.py:L289-L306](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_cache.py#L289-L306)）。

## 5. 综合实践

把本讲所有知识点串成一个「缓存诊断器」脚本。对下面三段管线各测一轮，记录每轮的 `cache_size()`、`current_cache_size_inbytes()` 与墙钟时间：

```python
# 示例代码：cache_diagnosis.py
import time, cvcuda, numpy as np

def probe(tag, make):
    cvcuda.clear_cache()
    t0 = time.perf_counter()
    for _ in range(200):
        obj = make()
        del obj
    dt = time.perf_counter() - t0
    print(f"{tag:<14} 条目={cvcuda.cache_size():>3} 字节={cvcuda.current_cache_size_inbytes():>12} 耗时={dt:.3f}s")

# A. 固定规格：稳态全命中
probe("固定规格", lambda: cvcuda.Tensor((1080, 1920, 3), np.float32, cvcuda.TensorLayout.HWC))

# B. 交替两种规格：仍然命中（两个键各自积累一个条目）
def alternating(i=[0]):
    i[0] ^= 1
    return cvcuda.Tensor((1080 + i[0], 1920, 3), np.float32, cvcuda.TensorLayout.HWC)
probe("交替两规格", lambda: alternating())

# C. 随机规格：永不命中，缓存无界增长直到整包清空
import random
probe("随机规格", lambda: cvcuda.Tensor((random.randint(1000, 1100), 1920, 3), np.float32, cvcuda.TensorLayout.HWC))
```

分析任务：

1. 解释 A/B/C 三段结束时条目数与字节数的差异（C 应远大于 B，B 约为 A 的两倍条目数）。
2. 对比 A 与 C 的耗时差异，估算单次 `cudaMalloc`+`cudaFree` 的代价。
3. 对 C 再跑一次加大循环次数的版本，观察字节数是否出现「锯齿」（爬到限额→瞬间跌落），记录锯齿周期。
4. 最后把 C 的循环改成 `_into` 风格（循环外预分配一个最大规格张量，循环内复用），对比耗时与最终字节数，验证 u3-l3 的结论：形状多变场景应改用预分配。

## 6. 本讲小结

- 对象缓存**只存在于 Python 绑定层**（`python/mod_cvcuda`）：`Tensor`、`Image`、`ImageBatchVarShape`、`TensorBatch`、算子对象都会进缓存；C/C++ 层没有缓存，`del` 不释放显存是缓存持有引用的直接结果。
- 命中条件是三层判定的合取：**同具体 Key 类型 + 同 CUDA 设备 + 规格相等**；对 Tensor 而言规格 = `TensorShape`（shape 含 layout 标签）+ `dtype`，`rowalign` 经由 Requirements 间接参与；变长批容器的键只有 capacity。
- **non-wrapped** 对象按真实字节记账、可被同规格创建复用（`CreateFromReqs` 的 fetch-or-create）；**wrapped** 对象按 0 字节记账、不可复用，进缓存只为在流上工作完成前保住外部显存的生命周期。
- 增长控制是「按设备字节限额 + 整包清空」：默认限额为每卡总显存一半；单对象超限不入缓存，累计超限则把该设备条目全部倒掉再插入——缓存曲线呈锯齿形，不是 LRU。
- 缓存实例**线程私有**、记账与限额**全局共享**；`clear_cache`/`cache_size` 支持 `ThreadScope.GLOBAL/LOCAL`；`clear_cache` 会先排空 ResourceGuard 的流回调再清理。
- 规格多变的循环（随机分辨率增强等）是缓存的天敌：永不命中 + 显存滞留 + 整包清空连坐，应改用 `_into` 变体预分配输出。

## 7. 下一步学习建议

下一讲 [u4-l3：多流、多线程与多 GPU](u4-l3-multi-stream-thread-gpu.md) 会把本讲的线程局部缓存与 u4-l1 的流模型放进真实并发场景：参考官方 `test_multi_threading.py`/`test_multi_gpu.py`，理解每线程的流栈、`ThreadScope` 在并发清理中的坑，以及多 GPU 下「设备号参与缓存键」带来的隔离行为。

想继续深挖源码的读者，建议按此顺序读：

1. [python/mod_cvcuda/nvcv/Image.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Image.cpp)——对比 Image 的 `Key{size, fmt}` 与 Tensor 的键有何不同，以及包装 Image 外壳复用的路径。
2. [python/mod_cvcuda/nvcv/Stream.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp)——`Stream` 居然也是缓存条目（`Stream::Key{}`），看看 `SynchronizeAndClearGCBag` 如何与缓存清理协作。
3. [python/mod_cvcuda/nvcv/CAPI.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/CAPI.cpp)——`ImplCache_Add`/`ImplCache_Fetch` 把缓存能力通过 C 函数表暴露给 cvcuda 算子层，是理解「算子对象为何算作 ExternalCacheItem」的钥匙。

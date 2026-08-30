# 多流、多线程与多 GPU

## 1. 本讲目标

学完本讲，你应该能够：

1. 用 `wait_stream` 把多条 `cvcuda.Stream` 组成链式或扇出依赖网，并对照官方测试 `test_multi_stream.py` 写出正确的多流代码。
2. 说出「当前流栈」（`StreamStack`）的真实实现：它是进程级单例 + 互斥锁，而不是每线程一份 —— 并理解这对多线程代码意味着什么。
3. 掌握多线程使用 CV-CUDA 的安全模式：显式传 `stream=`、每线程独立资源、理解每线程一张的对象缓存表与共享的配额账本。
4. 读懂资源守卫的锁模式：`Resource::submitSync` 里 per-resource 互斥锁下的四条同步路径（快路径 / 首次绑定 / 默认流哨兵 / 跨设备）。
5. 掌握多 GPU 的设备切换纪律：分配跟随「当前设备」、缓存键含设备号、事件不能跨设备、算子持久缓冲按设备重新分配。

本讲是第四单元（执行模型）的收官，把 u4-l1 的流模型与 u4-l2 的对象缓存放到「并发」这面放大镜下检验。

## 2. 前置知识

### 2.1 Python 的 GIL 与「线程安全」的边界

CPython 传统上有一把全局解释器锁（GIL，Global Interpreter Lock）：同一时刻只有一个线程执行 Python 字节码。但要特别注意两点：

- GIL 只序列化 **Python 字节码**，不序列化 GPU 上的异步工作。线程 A 提交的 kernel 还在 GPU 上飞，GIL 就已经切换给线程 B 了。
- pybind11 绑定函数在进入 C++ 后可能在耗时段落释放 GIL（例如 `Stream::sync` 里就有 `py::gil_scoped_release`），此时两个线程可以同时在 C++ 里跑。

所以「有 GIL，所以多线程随便写」是错误直觉。CV-CUDA 的做法是：把所有跨流、跨线程的同步决策收进绑定层的 C++ 代码里（资源守卫），Python 侧只需遵守少数几条纪律（本讲 4.2 节）。

### 2.2 C++ 的单例、`thread_local` 与读写锁

- **Meyers 单例**：函数内 `static X x;`，整个进程一份，C++11 保证其初始化线程安全。
- **`thread_local`**：每个线程各有一份独立副本。CV-CUDA 的对象缓存表就是 `thread_local` 的，而流栈是 Meyers 单例 —— 这个对比是本讲 4.1/4.2 节的主线之一。
- **`std::mutex` 与 `std::shared_mutex`**：前者独占；后者允许「多个读者同时进入、写者独占」（读写锁），用在读多写少的 per-device 表（辅助流表、事件表）上。

### 2.3 CUDA 的当前设备语义

CUDA runtime 里几乎所有 API（`cudaMalloc`、`cudaStreamCreate`、`cudaEventCreate`…）都作用于**当前设备**（由 `cudaGetDevice`/`cudaSetDevice` 决定）。换卡干活的标准姿势是「保存 → 切换 → 干活 → 恢复」。多 GPU 下最容易踩的坑就是：忘了切设备，结果显存分配到了别的卡上。

### 2.4 承接前两讲

- u4-l1 已建立：一切算子异步提交到流；`stream=` 参数缺省时由 `Stream::Current()` 兜底；`ResourceGuard` 在 kernel **之前**插入事件等待。本讲深挖它的内部锁实现。
- u4-l2 已建立：Python 对象缓存每线程一张表、配额按设备记账。本讲用多 GPU 测试验证「按设备隔离」这条性质。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tests/cvcuda/python/test_multi_stream.py` | 多流语义的官方测试：流上下文、`wait_stream` 链式/扇出/自等待 |
| `tests/cvcuda/python/test_multi_threading.py` | 多线程官方测试：并行 `submitStreamSync`、并行调用算子 |
| `tests/cvcuda/python/cvcuda_util.py` | 测试工具库，本讲关注 `run_parallel`（屏障 + 异常转发） |
| `tests/cvcuda/python/test_multi_gpu.py` | 多 GPU 官方测试：设备上报、跨设备资源搬运、缓存隔离、算子持久缓冲 |
| `python/mod_cvcuda/nvcv/StreamStack.cpp/.hpp` | 「当前流」栈：进程级单例 + 互斥锁 |
| `python/mod_cvcuda/nvcv/Stream.cpp` | Python `cvcuda.Stream` 的实现：`wait_stream`、`Current`、`activate`、辅助流、事件表 |
| `python/mod_cvcuda/nvcv/Resource.cpp` | 资源守卫的锁模式核心：`Resource::submitSync` 四条路径 |
| `python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp` | 算子 shim 里的守卫：`add` → `run` → `commit` |
| `python/mod_cvcuda/include/nvcv/python/Cache.hpp` | 缓存键基类 `IKey`：设备号参与哈希与相等比较 |
| `python/mod_cvcuda/nvcv/Cache.cpp` | `thread_local` 缓存实例 + 进程级账本；`clear_cache(scope)` |
| `python/mod_cvcuda/nvcv/ThreadScope.cpp` | `ThreadScope.GLOBAL / LOCAL` 枚举导出 |
| `python/mod_cvcuda/operators/OpFlip.cpp` | 一个算子 shim 的守卫用法样板（本讲的参照代码） |

## 4. 核心概念与源码讲解

本讲四个最小模块：多流组网与流栈、多线程、资源守卫的锁模式、多 GPU。

### 4.1 多流并发：wait_stream 组网与流栈的真相

#### 4.1.1 概念说明

单流上所有 kernel 严格串行。多流的价值在于**重叠**：当一段工作受限于拷贝带宽或 CPU 提交速度时，另一段计算可以同时在别的流上跑。理想情况下两段完全并行的工作，墙钟时间从 \( T_1 + T_2 \) 降为 \( \max(T_1, T_2) \)。

但流与流之间默认**没有任何顺序保证**。一旦两条流要读写同一块显存，就必须显式建立依赖。CV-CUDA 提供的组网原语是 `Stream.wait_stream(other)`：

> 让本流**后续**入队的工作，等待 `other` 流**当前已入队**的全部工作完成。

注意时态：它只管「截至调用时刻已经入队」的工作，之后 `other` 上再提交的任务不受约束 —— 这与 u4-l1 讲过的 `ResourceGuard` 自动记账互补：手工 `wait_stream` 管你已经知道的生产者-消费者边，守卫管包装张量/跨库带来的隐式边。

#### 4.1.2 核心流程

**链式（pipeline）组网**：N 条流首尾相接，数据像接力棒一样传下去：

```text
初始化: prev = None
for stream in [s1, s2, s3, s4]:
    if prev is not None:
        stream.wait_stream(prev)      # 本流等待上一条流已入队的工作
    op_into(dst, src, stream=stream)  # 在本流上消费
    op_into(src, dst, stream=stream)  # 乒乓交换
    prev = stream
最后: streams[-1].sync() 再拷回主机校验
```

**扇出（fan-out）组网**：一条生产者流，多条消费者流各自 `wait_stream(producer)` 后并行读同一份中间结果：

```text
s1: flip_into(scratch, input)          # 生产者写 scratch
s2.wait_stream(s1); s3.wait_stream(s1)  # 两个消费者都等生产者
s2: flip_into(out2, scratch)            # 两路并行读
s3: flip_into(out3, scratch)
s2.sync(); s3.sync() 后各自校验
```

`wait_stream` 的底层是一次事件三步曲：在对方流上 `cudaEventRecord`，再对本流 `cudaStreamWaitEvent`，事件是 `cudaEventDisableTiming` 的（不计时机，只做同步，开销更低）。

#### 4.1.3 源码精读

**（1）多流的基本事实：每 `cvcuda.Stream()` 一个新对象，`current` 缺省是 `default`。**

[tests/cvcuda/python/test_multi_stream.py:L23-L33](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_stream.py#L23-L33) 创建三条流并断言它们互不相同、且不写 `with` 时 `Stream.current is Stream.default`。`with` 嵌套的压栈/弹栈语义在 [tests/cvcuda/python/test_multi_stream.py:L46-L57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_stream.py#L46-L57) 验证：离开内层回到外层，全部离开回到 default。异常路径下栈也能正确弹出（[tests/cvcuda/python/test_multi_stream.py:L64-L77](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_stream.py#L64-L77)），因为 `__exit__` 是 `with` 语句保证调用的。

**（2）`wait_stream` 的实现：事件三步曲 + 自等待短路。**

[python/mod_cvcuda/nvcv/Stream.cpp:L457-L470](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L457-L470) 是 `Stream::wait_stream` 的全部逻辑：先判断 `other->handle() == m_handle` 直接返回（等待自己是 no-op，对应测试 [tests/cvcuda/python/test_multi_stream.py:L186-L189](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_stream.py#L186-L189)，避免死锁）；否则释放 GIL 后，在对方流上记录事件、让本流等待该事件。绑定时声明的文档也明确「跨设备使用不受支持，会抛 CUDA 错误」（[python/mod_cvcuda/nvcv/Stream.cpp:L860-L867](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L860-L867)）。

**（3）链式组网的官方范式。**

[tests/cvcuda/python/test_multi_stream.py:L115-L143](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_stream.py#L115-L143) 用 4 条流跑 50 轮乒乓翻转：核心循环在 [tests/cvcuda/python/test_multi_stream.py:L132-L139](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_stream.py#L132-L139) —— 每条流先 `wait_stream(prev_stream)` 再做两次 `flip_into`（flipCode=-1 双轴翻两次等于复原）。注意它**全程用 `flip_into` 显式指定输出**，因为 allocating 变体每轮会新建输出张量，乒乓就演不下去了。50 轮 × 4 流只 `sync` 最后一条流即可：依赖链保证了传递性。最终断言数据完全复原（[tests/cvcuda/python/test_multi_stream.py:L141-L143](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_stream.py#L141-L143)）。同文件的 [L146-L183](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_stream.py#L146-L183) 是「重载版」：stream1 先压满大量工作再产出，stream2 等它，验证等待的是**全部已入队工作**而非最近一个 kernel。

**（4）扇出组网的官方范式。**

[tests/cvcuda/python/test_multi_stream.py:L192-L221](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_stream.py#L192-L221)：stream1 写 scratch，stream2/stream3 各自 `wait_stream(stream1)` 后并行做第二次翻转，最后两条流分别 `sync` 并断言输出都等于原图。这是「一份数据多路消费」的最小正确样板。

**（5）流栈的真相：进程级单例，不是每线程一份。**

`Stream::Current()` 取栈顶（[python/mod_cvcuda/nvcv/Stream.cpp:L472-L485](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L472-L485)）。而这个栈的来源在 [python/mod_cvcuda/nvcv/StreamStack.cpp:L49-L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/StreamStack.cpp#L49-L53)：`static StreamStack stack;` —— Meyers 单例，**整个进程一份**。成员是 `std::stack<std::weak_ptr<Stream>>` 加一把 `std::mutex`（[python/mod_cvcuda/nvcv/StreamStack.hpp:L39-L41](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/StreamStack.hpp#L39-L41)）；push/pop/top 三个操作各自持锁（[python/mod_cvcuda/nvcv/StreamStack.cpp:L24-L47](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/StreamStack.cpp#L24-L47)），`top()` 返回前还要把 `weak_ptr` 提升成 `shared_ptr`，避免返回瞬间被别的线程析构。

这把互斥锁保证的是**内存安全**（不会因数据竞争崩溃），**不保证线程隔离**：如果线程 A 刚 `with s1:` 压栈，GIL 切到线程 B 又 `with s2:` 压栈，那么 A 随后调用的算子读到的 `current` 是 s2。Python 侧静态属性文档写着 "Get the current CUDA stream for this thread"（[python/mod_cvcuda/nvcv/Stream.cpp:L842-L844](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L842-L844)），但实现层面它读到的是进程共享栈的栈顶。**工程结论：多线程代码里不要依赖 `with`/`current`，一律显式传 `stream=`** —— 后面 4.2 会看到官方多线程测试正是这么做的。

另外注意模块初始化时会把包装 legacy 0 号流的 `Stream.default` 压进这个共享栈作为栈底（[python/mod_cvcuda/nvcv/Stream.cpp:L846-L849](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L846-L849)）；解释器退出时 `CleanupAtExit` 会检查「栈里只剩这条全局流」，否则打印 `Stream stack leak detected`（[python/mod_cvcuda/nvcv/Stream.cpp:L717-L727](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L717-L727)）—— `with` 忘写 `__exit__` 之类的泄漏在这里现形。

#### 4.1.4 代码实践

**实践目标**：亲手验证扇出组网的正确性，并体验多流是否真带来重叠。

**操作步骤**（示例代码，仿照官方测试改写）：

```python
# multi_stream_demo.py —— 示例代码，改写自 tests/cvcuda/python/test_multi_stream.py
import numpy as np
import cupy
import cvcuda

N, H, W, C = 4, 1024, 1024, 3
rng = np.random.default_rng(0)

src_np = rng.integers(0, 256, (N, H, W, C), dtype=np.uint8)
src = cvcuda.as_tensor(cupy.asarray(src_np), "NHWC")
scratch_arr = cupy.zeros((N, H, W, C), dtype=np.uint8)
out2_arr = cupy.zeros((N, H, W, C), dtype=np.uint8)
out3_arr = cupy.zeros((N, H, W, C), dtype=np.uint8)
scratch = cvcuda.as_tensor(scratch_arr, "NHWC")
out2 = cvcuda.as_tensor(out2_arr, "NHWC")
out3 = cvcuda.as_tensor(out3_arr, "NHWC")

s1, s2, s3 = cvcuda.Stream(), cvcuda.Stream(), cvcuda.Stream()

# 扇出：s1 生产，s2/s3 并行消费
cvcuda.flip_into(scratch, src, -1, stream=s1)
s2.wait_stream(s1)
s3.wait_stream(s1)
cvcuda.flip_into(out2, scratch, -1, stream=s2)
cvcuda.flip_into(out3, scratch, -1, stream=s3)
s2.sync()
s3.sync()

np.testing.assert_array_equal(cupy.asarray(out2.cuda()).get(), src_np)
np.testing.assert_array_equal(cupy.asarray(out3.cuda()).get(), src_np)
print("fan-out ok")
```

然后做一组对照：把 `stream=s2/s3` 全部换成 `stream=s1`（单流串行），用 `time.perf_counter()` 包住从第一次 flip 到 sync 的墙钟时间比较两版（各预热几轮再计时）。

**需要观察的现象**：断言通过（双翻复原）；多流版本墙钟时间不劣于单流版本。

**预期结果**：正确性必然通过；性能上 flip 这类纯带宽型 kernel 重叠收益有限（两条流抢同一份显存带宽），多流收益更多出现在「计算 + 拷贝」或「计算 + 编码」这类异质段之间。具体加速比**待本地验证**（依赖 GPU 型号与驱动）。

#### 4.1.5 小练习与答案

**练习 1**：把 4.1.4 的扇出代码里 `s2.wait_stream(s1)` 这一行删掉，会发生什么？一定出错吗？

**答案**：不保证一定出错。删掉后 s2 与 s1 之间没有顺序约束，s2 的 kernel 可能在 s1 写完 scratch 之前就读它，结果是读到了旧值/半新半旧值 —— 表现为断言随机失败（数据竞争是概率性的，小图高频跑可能「侥幸」通过多次后突然失败）。这正是官方测试要显式 `wait_stream` 的原因；同理 `s3` 的等待也不能省。

**练习 2**：`stream.wait_stream(stream)`（自己等自己）为什么必须实现成 no-op 而不是直接记录事件？

**答案**：若不短路，会在本流上记录一个事件再让本流等待它 —— 流是严格 FIFO 的，等待一个只有排到后面才会触发的事件就形成死锁（实际上同一流内事件在等待入队前已记录完成，CUDA 对此有特判，但语义上依赖实现细节不可靠）。源码在 [python/mod_cvcuda/nvcv/Stream.cpp:L463-L464](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L463-L464) 用 `if (other->handle() == m_handle) return;` 显式短路，官方测试 [tests/cvcuda/python/test_multi_stream.py:L186-L189](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_stream.py#L186-L189) 注释写明 "must be a no-op, not a deadlock"。

**练习 3**：`StreamStack` 为什么存 `weak_ptr` 而不是 `shared_ptr`？

**答案**：若存 `shared_ptr`，栈本身会一直持有流的引用计数 —— 用户 `del` 掉一个 `cvcuda.Stream` 对象后它仍被栈拽着无法析构，`cudaStreamDestroy` 永远不执行，造成流句柄泄漏。存 `weak_ptr` 只观察不拥有，`top()` 时用 `.lock()` 临时提升：提升失败（栈顶已被销毁）就返回 `nullptr`，由 `Stream::Current()` 走空栈分支。

### 4.2 多线程并发：GIL、线程局部缓存与官方并行测试

#### 4.2.1 概念说明

多线程使用 CV-CUDA 的三个事实构成本模块：

1. **流栈是共享的**（4.1 已证），所以多线程下的当前流语义不可靠，必须显式传 `stream=`。
2. **对象缓存表是每线程一份的**：`Cache::Instance()` 返回 `thread_local` 变量。线程 A 缓存的张量，线程 B 永远复用不到 —— N 个工作线程就是 N 张独立表、各自占配额。
3. **跨线程共享同一个 Tensor/ImageBatch 对象是允许的**，其安全性来自每个 `Resource` 内部的互斥锁（4.3 详解）与流记账；但**共享输出缓冲**意味着两条流上的 kernel 写同一块显存，GPU 层面的写入竞争需要你自己用流依赖来排除。

官方把「哪些操作被证明线程安全」固化成了两个并行测试，其中一个的文档串直接点明了历史教训：全局缓存曾在禁用 GIL 的解释器下崩溃。

#### 4.2.2 核心流程

官方并行测试的执行骨架（`run_parallel`）：

```text
1. nb_threads = len(os.sched_getaffinity(0))     # 取 CPU 亲和核数作为线程数
2. 创建 nb_threads 个 threading.Thread + 一个 Barrier
3. 每个线程: barrier.wait()  → 尽量同时进入目标函数
   目标函数抛异常 → 存入闭包，不让线程带崩
4. join 全部线程；若有异常 → 在主线程重新抛出
```

用屏障让线程「同时起跑」，是把竞争窗口拉到最大的测试手法 —— 比各线程随手开跑更容易暴露问题。

#### 4.2.3 源码精读

**（1）`run_parallel` 工具。**

[tests/cvcuda/python/cvcuda_util.py:L359-L396](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_util.py#L359-L396) 是完整实现：线程数取自 `os.sched_getaffinity(0)`（[L382](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_util.py#L382)），屏障在 [L386](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_util.py#L386)，异常经闭包转发到主线程（[L377-L380](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_util.py#L377-L380) 与 [L395-L396](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_util.py#L395-L396)）—— 这样失败时 pytest 能给出正常的 traceback 而不是线程里的哑异常。

**（2）并行提交同步：`Resource::submitSync` 的线程安全测试。**

[tests/cvcuda/python/test_multi_threading.py:L23-L32](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_threading.py#L23-L32) 是最小也最尖锐的并行测试：一个 `cvcuda.Tensor` 被所有线程共享，每线程循环 10000 次调用 `resource.submitStreamSync(cvcuda.Stream())` —— 每次都换一条新流，强制走 4.3 节的慢路径（换流必须记录事件并重绑）。它验证的就是 per-resource 互斥锁在高压下的正确性。`submitStreamSync` 这个 Python 方法名在 [python/mod_cvcuda/nvcv/Resource.cpp:L265-L275](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L265-L275) 注册，映射到 C++ 的 `Resource::submitSync`。

**（3）并行调用算子：缓存与 GIL 的联合考验。**

[tests/cvcuda/python/test_multi_threading.py:L108-L126](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_threading.py#L108-L126) 让每个线程对**同一个输入**调用 allocating 版 `cvcuda.bndbox` 并把输出收集进共享列表（`outputs.append` 依赖 append 的线程安全性），最后逐个断言元数据一致。文档串（[L110-L113](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_threading.py#L110-L113)）记录了它的存在意义："With a global cache, the bndbox operator crashes when used in parallel with the GIL disabled." —— 缓存曾经是全局单例，在 nogil 解释器下多线程同时 fetch/add 会崩；现在改成每线程一张表后此测试守护着这个性质。

**（4）缓存的线程结构：表隔离、账共享。**

[python/mod_cvcuda/nvcv/Cache.cpp:L376-L380](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L376-L380)：`thread_local Cache cache;` —— 每线程一个 Cache 实例、各自的条目表（`Impl::items` 是普通成员，[python/mod_cvcuda/nvcv/Cache.cpp:L88-L94](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L88-L94)）。同一段落里 `mtx`、`cache_limit_inbytes`、`current_size_inbytes` 却是 `inline static` —— 进程级：配额账本全局共享，所以 `GLOBAL` 作用域的 `clear_cache` 要拿着这把锁把**所有线程**的表合并清空（[python/mod_cvcuda/nvcv/Cache.cpp:L382-L391](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L382-L391)）。`ThreadScope` 枚举本体在 [python/mod_cvcuda/nvcv/ThreadScope.cpp:L22-L27](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ThreadScope.cpp#L22-L27) 导出为 `GLOBAL/LOCAL`；`clear_cache`/`cache_size` 按 scope 分派（[python/mod_cvcuda/nvcv/Cache.cpp:L451-L464](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Cache.cpp#L451-L464)），且清理前先 `Stream::SynchronizeAndClearGCBag()` 排干辅助流回调（承接 u4-l2）。

#### 4.2.4 代码实践

**实践目标**：确认「每线程独立资源 + 显式传流」的多线程模式正确工作，并观察每线程缓存的显存放大效应。

**操作步骤**（示例代码）：

```python
# threads_demo.py —— 示例代码
import threading
import numpy as np
import cupy
import cvcuda

H, W, C = 512, 512, 3

def worker(thread_no: int, barrier: threading.Barrier, results: list):
    stream = cvcuda.Stream()                     # 每线程自己的流
    rng = np.random.default_rng(thread_no)
    src_np = rng.integers(0, 256, (H, W, C), dtype=np.uint8)
    src = cvcuda.as_tensor(cupy.asarray(src_np), "HWC")
    dst = cvcuda.Tensor((H, W, C), dtype=cvcuda.Type.U8, layout="HWC")  # 本线程独享输出
    barrier.wait()
    for _ in range(200):
        cvcuda.flip_into(dst, src, 0, stream=stream)   # 显式传 stream，不用 with/current
    stream.sync()
    got = cupy.asarray(dst.cuda()).get()
    results[thread_no] = np.array_equal(got, np.flip(src_np, axis=0))  # flipCode=0 上下翻

nb = 4
barrier = threading.Barrier(nb)
results = [None] * nb
threads = [threading.Thread(target=worker, args=(i, barrier, results)) for i in range(nb)]
for t in threads:
    t.start()
for t in threads:
    t.join()
print(results)   # 期望 [True, True, True, True]
```

跑完后在循环次数从 200 改成 1 的两个版本里分别观察 `cvcuda.cache_size(cvcuda.ThreadScope.LOCAL)`（在主线程打印只能看到主线程的表，体会「表隔离」）或用 `nvidia-smi` 看进程显存。

**需要观察的现象**：四个线程全部校验为 True；无异常、无崩溃。

**预期结果**：正确性稳定通过（每线程资源隔离 + 显式流，是官方测试同款模式）。显存占用约为单线程的数倍（每线程各自的输出张量与缓存表），具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么官方多线程测试里的 worker 从不写 `with stream:`，而是把流作为参数传给算子？

**答案**：因为「当前流栈」是进程共享的（4.1.3 第 (5) 点）。线程 A 压栈 s1 后、调用算子前，线程 B 可能压入 s2，A 的算子就会错误地落到 s2 上。显式 `stream=` 参数从 Python 一路穿透到 kernel 启动（u4-l1 讲过它中途不被偷换），不受共享栈的干扰。

**练习 2**：4 个工作线程各自循环调用 allocating 版 `cvcuda.flip`（不用 `_into`），相比单线程，显存与配额会怎么变？

**答案**：每线程有独立的 `thread_local` 缓存表，同形状的输出张量会在**每张表里各缓存一份**，显存占用约乘以线程数；配额账本虽然是全局共享的（`inline static`），但条目互不复用，等于放大了缓存足迹。改用 `flip_into` + 线程外预分配的输出，或在线程入口 `cvcuda.clear_cache(cvcuda.ThreadScope.LOCAL)` 控制，可以抑制这种放大（呼应 u4-l2 的 unbounded growth）。

**练习 3**：`run_parallel` 里 `barrier.wait()` 如果去掉，测试还「有效」吗？

**答案**：仍然能跑，但有效性下降。没有屏障时各线程起跑时刻离散，竞争窗口变小、重叠度降低，数据竞争类 bug 更容易漏检。屏障把所有线程对齐到同一时刻进入临界区，是压测并发的常用强化手段；同时它也保证「被测函数真正并发执行」这一前提成立。

### 4.3 资源守卫的锁模式：Resource::submitSync 的四条路径

#### 4.3.1 概念说明

u4-l1 讲过 ResourceGuard 的对外行为：算子 shim 在提交 kernel **之前**为每个输入/输出资源插入跨流等待。本模块下钻一层，看这份「记账」的数据结构与锁。

每个 Python 可见的数据对象（Tensor、ImageBatch、算子句柄……）都继承自 `Resource`，内部维护一份「我上一次被提交到哪条流、哪个设备」的记录（`m_lastStream` / `m_lastStreamHandle` / `m_lastDevice`），全部读写都由一把 per-resource 的 `std::mutex` 保护。**锁模式**即指：

- 粒度：锁属于单个资源，不属于全局表 —— 多线程操作不同资源互不阻塞。
- 协议：进锁 → 比较「上次的流」与「这次的流」→ 分四条路径处理 → 更新记录 → 出锁。

这就是 [tests/cvcuda/python/test_multi_threading.py:L23-L32](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_threading.py#L23-L32) 并行轰炸的对象。

#### 4.3.2 核心流程

`Resource::submitSync(stream)` 的判定树：

```text
加锁 m_mtx
├─ 上次流 == 本次流?                        → 【路径 0: 快路径】直接返回，零 CUDA 调用
├─ 没有上次的流（首次提交）?                  → 【路径 1: 记账】记录本次流/设备，返回
├─ 上次流是 legacy/PerThread 哨兵值(1 或 2)?  → 【路径 2: 保守全同步】cudaDeviceSynchronize
│    （跨设备时先切到旧设备再同步再切回）
├─ 上次设备 ≠ 当前设备?                      → 【路径 3: 跨设备】切到旧设备 cudaStreamSynchronize 旧流，切回
└─ 其他（同设备不同流）                       → 【路径 4: 事件】旧流 cudaEventRecord + 新流 cudaStreamWaitEvent
更新 m_lastStream / m_lastDevice 为本次
解锁
```

设计动机：事件等待只能排进流队列，是 O(1) 的异步操作，首选；但它**不能跨设备**（CUDA 事件不可跨设备等待），也不能可靠覆盖「生产者谎报了流」的场景，所以那两种情况退化为重量级同步。

#### 4.3.3 源码精读

**（1）算子 shim 里的守卫用法（锁模式的调用方）。**

[python/mod_cvcuda/operators/OpFlip.cpp:L44-L50](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L44-L50)：`ResourceGuard guard(*pstream)` → `add(READ, {input})` / `add(WRITE, {output})` / `add(NONE, {*Flip})` → `guard.run([...]{ Flip->submit(...); })`。`run()` 在调用 submit 之前先做同步屏障 —— [python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp:L101-L126](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp#L101-L126) 的注释说得非常清楚：`cudaStreamWaitEvent` 只约束**排在其后**的同流命令，所以必须先于 kernel 执行；头文件 [L94](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp#L94) 还给出了推荐写法模板。

**（2）锁与快路径。**

[python/mod_cvcuda/nvcv/Resource.cpp:L73-L104](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L73-L104)：函数第一行 `std::unique_lock lk(m_mtx)` 拿住资源自己的锁；[L91-L104](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L91-L104) 是快路径 —— 上次流与本次流相同就直接返回。内联注释（[L91-L100](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L91-L100)）给出了量级：这条路径每次只有「mutex + 指针比较」，不碰 CUDA 驱动；否则一次 `cudaGetDevice` 就要几微秒，一个算子锁 5~7 个资源就是几十微秒的固定开销。**这就是稳态单流管线几乎感觉不到守卫存在的原因。**

**（3）首次绑定与默认流哨兵。**

[L111-L118](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L111-L118) 首次提交只记账不同步。[L120-L150](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L120-L150) 是最有讲究的一段：若「上次的流」是 `cudaStreamLegacy`(1) 或 `cudaStreamPerThread`(2) 这类哨兵值，说明它来自 CAI 协议的解析（生产者没报流，或报了个哨兵），而这类生产者**可能说谎** —— 事件屏障拦不住非阻塞流上的工作，所以退化为 `cudaDeviceSynchronize` 求绝对正确。注释特意说明：cvcuda 自己产出的都是真实流指针，绝不会是 1 或 2，因此 cvcuda→cvcuda 链始终走轻量的事件路径，这条重路径只在包装缓冲的第一次使用时触发一次。

**（4）跨设备与事件路径。**

[L151-L167](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L151-L167)：跨设备时「保存设备 → 切旧设备 → `cudaStreamSynchronize(旧流)` → 切回」（CUDA 事件不能跨设备，只能这样重同步）；同设备不同流才是事件三步曲。最后 [L169-L173](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L169-L173) 把记账换绑到新流。另外 [L214-L227](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L214-L227) 的 `seedLastStream` 是 u2-l4 提过的「生产者流记账」：包装外部数组时登记生产者流，且只在尚未被 cvcuda 占有时生效 —— 它是哨兵路径的入口之一。

**（5）守卫的另一半：把资源「押」到流上保活。**

kernel 提交后，`guard.run()` 的 `commit()` 调 `Stream_HoldResources`（[python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp:L128-L171](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp#L128-L171)），最终落到 [python/mod_cvcuda/nvcv/Stream.cpp:L543-L611](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L543-L611) 的 `holdResources`：它把资源引用装进闭包，在**辅助流**上排一个主机回调，等主流工作完成后再释放。注释里的时间线图（[L571-L585](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L571-L585)）解释了为什么必须绕道辅助流：若把回调直接排进主流，GPU 会停下来等 CPU 执行回调，主流上后续 kernel 全被拖住。这个机制在并发场景是正确性的最后一环：**异步 kernel 还在飞的时候，Python 侧提前 `del` 掉的张量不会被真正释放**。辅助流与事件都按设备建表、用 `std::shared_mutex` 读写锁保护（`GetAuxStream` [L291-L325](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L291-L325)，`getEvent` [L520-L541](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L520-L541)），进程级静态成员见 [L48-L51](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L48-L51)。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：对给定的调用序列，准确判定每个资源各走哪条同步路径。

**操作步骤**：阅读 [python/mod_cvcuda/nvcv/Resource.cpp:L73-L174](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L73-L174)，然后为下面每段代码写出 `src`、`dst` 两个资源各自命中的路径编号（0 快路径 / 1 首次绑定 / 2 哨兵全同步 / 3 跨设备 / 4 事件）：

```python
# (a)
src = cvcuda.as_tensor(cupy.asarray(host_np), "HWC")   # cupy 数组经 CAI 包装
dst = cvcuda.Tensor((H, W, C), dtype=cvcuda.Type.U8, layout="HWC")
with s1:
    cvcuda.flip_into(dst, src, 0, stream=s1)

# (b) 紧接 (a) 之后
cvcuda.flip_into(dst, src, 0, stream=s1)

# (c) 紧接 (b) 之后
cvcuda.flip_into(dst, src, 0, stream=s2)   # s2 是另一条 cvcuda.Stream
```

**需要观察的现象**：能否把「生产者是谁」「上一次流是什么」这两个问题对号入座。

**预期结果（参考判定，可对照源码自行验证）**：
- (a) `src`：cupy 数组是外部生产者，`seedLastStream` 登记的流是 cupy 当前流（常为 None/哨兵语义），首次被 cvcuda 提交时命中**路径 2 或 1**（取决于 CAI 上报的流是否为哨兵）；`dst`：cvcuda 自建张量从未提交过，命中**路径 1**。
- (b) 两者上次流都是 s1、本次也是 s1，全部命中**路径 0**（这就是稳态管线的零开销来源）。
- (c) 上次流 s1 ≠ 本次 s2 且同设备，命中**路径 4**（事件三步曲）。
若把 (c) 换成在另一张 GPU 上执行（先 `cupy.cuda.Device(1).use()`），则命中**路径 3**。以上判定基于当前 HEAD 源码逻辑推演，具体哨兵取值**待本地验证**（可用算子包装开销计时间接观察路径 2 的代价）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `submitSync` 的锁挂在每个 `Resource` 上，而不是像缓存那样做一张全局锁表？

**答案**：算子每次调用要锁 5~7 个资源，若锁是全局的，多线程并发调用任何算子都会在这把锁上完全串行，等于把并发吃光；per-resource 锁让「不同线程操作不同张量」完全并行，只有真正共享同一对象时才互斥（正是 test_parallel_resource_submit 轰炸的场景）。缓存则相反：条目本来就按线程隔离，全局的只有账本，锁冲突窗口极小。

**练习 2**：路径 2（哨兵 → `cudaDeviceSynchronize`）代价很高，为什么不做得更聪明？

**答案**：因为它面对的是「不可信信息」：CAI v2 生产者根本没报流（解析时兜底成 legacy 哨兵），或生产者报的流与其真实写入流不符。事件等待只能覆盖已知流上已入队的工作，对「不知道在哪儿的工作」无能为力；全设备同步是唯一不需要任何假设的正确做法。源码注释也说明了缓解方式：该路径每个包装缓冲只触发一次，之后 `m_lastStream` 已是真实 cvcuda 流，回到事件路径（[python/mod_cvcuda/nvcv/Resource.cpp:L134-L136](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L134-L136)）。

**练习 3**：如果去掉 `holdResources` 的辅助流绕道，把释放回调直接排进主流，会怎样？

**答案**：主流上的下一个 kernel 必须等主机回调执行完才能开跑（CUDA 流回调执行期间流是停摆的），GPU 出现气泡；而回调本身还要等 GIL 等资源，气泡可能很大。绕道辅助流后，主流只排一个事件记录，GPU 连续执行，CPU 在旁边异步完成回收。这是典型的「用第二条流隔离慢速 CPU 工作」的模式，与用户层多流重叠是同一个思想。

### 4.4 多 GPU：设备切换、按设备隔离的缓存与算子内部缓冲

#### 4.4.1 概念说明

多 GPU 使用 CV-CUDA 的纪律可以归纳成四条：

1. **分配跟随当前设备**：`cvcuda.Tensor(...)`、`cvcuda.Image(...)` 在调用线程的当前设备上分配（多 GPU 测试专门验证了这一点）。
2. **缓存按设备隔离**：缓存键里揉进了设备号 —— 设备 0 缓存的张量永远不会在设备 1 上被复用。
3. **跨设备没有事件捷径**：CUDA 事件不能跨设备等待，所以换卡使用同一资源时会退化为重量级同步（4.3 的路径 3），跨设备搬运数据要显式走 `cupy.array(...)`（隐式经主机）之类的通道。
4. **算子的持久缓冲按设备重分配**：不少算子（gaussian、rotate、inpaint、hq_resize、bndbox…）内部持有跨调用复用的设备缓冲，这些缓冲必须跟着当前设备走，否则会撞上 `cudaErrorIllegalAddress`。

官方用一个 autouse fixture 保证每个测试结束后切回设备 0，避免污染后续测试 —— 这本身就是「当前设备是全局状态」的最好提醒。

#### 4.4.2 核心流程

一个典型的「GPU0 产出 → GPU1 消费」流程：

```text
1. cupy.cuda.Device(0).use()          # 切到设备 0
2. 在设备 0 上创建输入张量、流，跑算子得 intermediate
3. stream0.sync()                     # 确保产出就绪
4. cupy.cuda.Device(1).use()          # 切到设备 1
5. gray_on_0 = cupy.asarray(intermediate.cuda())   # 取设备 0 上的视图
6. gray_on_1 = cupy.array(gray_on_0)  # 跨设备拷贝（经主机或对等拷贝）
7. 在设备 1 上继续包装、执行算子
```

多 GPU 测试的门槛控制：

```text
NUM_GPUS = cupy.cuda.runtime.getDeviceCount()
requires_multi_gpu = pytest.mark.skipif(NUM_GPUS < 2, reason="...")
@t.fixture(autouse=True)
def _restore_default_device():        # 每个测试后强制回设备 0
    yield
    cupy.cuda.Device(0).use()
```

#### 4.4.3 源码精读

**（1）测试门槛与设备复位。**

[tests/cvcuda/python/test_multi_gpu.py:L22-L27](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L22-L27) 用 `cupy.cuda.runtime.getDeviceCount()` 数卡，少于 2 张就跳过多 GPU 用例；[L30-L34](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L30-L34) 的 autouse fixture 在 `yield` 之后把设备切回 0 ——「先测后复位」的 fixture 骨架值得直接抄进自己的多 GPU 脚本。

**（2）跨设备接力：资源在两卡间搬运。**

[tests/cvcuda/python/test_multi_gpu.py:L131-L162](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L131-L162)（`test_resource_reuse_across_gpus`）：设备 0 上跑 `cvtcolor` 得到 `intermediate`，切到设备 1 后用 `cupy.asarray(intermediate.cuda())` 取视图、`cupy.array(...)` 做跨设备拷贝，再拼回 RGB 在设备 1 上跑第二次 `cvcvtColor`。它示范了两件事：cvcuda 不会偷偷帮你搬数据（必须显式拷）；同一 Python 对象在新设备上继续使用时，守卫会走 4.3 的跨设备路径完成同步。设备正确性则由 [L42-L66](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L42-L66)（单卡冒烟）与 [L74-L94](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L74-L94)（DLPack 导出必须上报真实设备而非写死 0）共同守护。

**（3）缓存隔离的机制：设备号进键。**

缓存键的基类 [python/mod_cvcuda/include/nvcv/python/Cache.hpp:L31-L78](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/Cache.hpp#L31-L78)：构造函数里 `cudaGetDevice(&m_deviceId)` 抓住**建键时刻的当前设备**（[L34-L37](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/Cache.hpp#L34-L37)）；哈希把设备号揉进去（[L47-L58](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/Cache.hpp#L47-L58)）；`operator==` 在设备号不同时直接判不相等（[L60-L71](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/Cache.hpp#L60-L71)）—— 连 `doIsCompatible` 都不用比。行为验证在 [tests/cvcuda/python/test_multi_gpu.py:L238-L273](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L238-L273)：设备 0 建 Tensor、`del`、切设备 1 再建同形状 —— 断言两份的设备指针不同且新份确在设备 1 上（缓存的旧份绝不能跨设备复用）。各卡结果的一致性则由 [L165-L189](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L165-L189) 逐卡跑同一算式再 `assert_array_equal` 验证。

**（4）激活流时的设备校验。**

`with stream:` 压栈前会检查「流是在哪张卡上创建的」与「当前设备」是否一致，不一致直接抛 `StreamError`（[python/mod_cvcuda/nvcv/Stream.cpp:L487-L501](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L487-L501)）。这把「拿设备 0 的流往设备 1 的数据上提交」这类错误提前到 CPU 侧报错，而不是留下一个难查的 GPU 异步错误。配套地，`GetAuxStream`/`getEvent` 的 per-device 表用 `shared_mutex` 实现读多写少的并发访问（[python/mod_cvcuda/nvcv/Stream.cpp:L291-L325](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L291-L325)、[L520-L541](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L520-L541)）：已存在走共享锁并发读，不存在才升级独占锁插入一次 —— 与 4.3 的 per-resource 锁共同构成绑定层的并发骨架。

**（5）算子持久缓冲的按设备正确性。**

[tests/cvcuda/python/test_multi_gpu.py:L277-L308](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L277-L308) 起的注释块点明主题：「带持久 GPU 缓冲的算子必须能在非默认设备上工作而不崩（`cudaErrorIllegalAddress`）」。公共骨架 `_run_op_on_each_gpu` 在 [L284-L290](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L284-L290)，随后五个用例各盯一个高危算子：gaussian（持久 `m_kernel`，[L293-L308](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L293-L308)）、rotate（持久 `d_aCoeffs`，[L311-L326](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L311-L326)）、inpaint、hq_resize、bndbox（[L329-L402](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L329-L402)）。另外 [L202-L230](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L202-L230) 验证 `Image.zeros`/`Image()`/`Tensor()` 都分配在**当前**设备而非写死的设备 0 —— 这是纪律 1 的直接测试。

#### 4.4.4 代码实践

**实践目标**：在没有多卡的环境也能跑通可跑的部分，并准备好双卡清单。

**操作步骤**：

1. 单卡环境运行：`pytest tests/cvcuda/python/test_multi_gpu.py -v`（需按 `tests/README.md` 配好环境）。观察输出：单卡冒烟用例 `test_dlpack_device_id_on_default_gpu` 执行，其余带 `requires_multi_gpu` 标记的用例显示 `SKIPPED (Multi-GPU tests require at least 2 GPUs)`。
2. 阅读并抄下两段骨架：autouse 设备复位 fixture（[tests/cvcuda/python/test_multi_gpu.py:L30-L34](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L30-L34)）与 `_run_cvtcolor_on_gpu` 的「切卡 → 建流 → 跑 → 同步 → 返回」五步（[tests/cvcuda/python/test_multi_gpu.py:L97-L112](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L97-L112)）。
3. 若有双卡：跑 `pytest tests/cvcuda/python/test_multi_gpu.py -k "cache_no_cross_device or resource_reuse" -v`，确认通过。
4. 若有双卡，再做一个小实验（示例代码）：在设备 0 上 `t0 = cvcuda.Tensor((8,8,3), np.uint8, "HWC")` 后 `del t0`，切设备 1 建同形状 `t1`，打印 `cupy.asarray(t1.cuda()).device.id` —— 应为 1。

**需要观察的现象**：单卡时 SKIPPED 的原因字符串与源码一致；双卡时隔离与接力用例全部通过。

**预期结果**：步骤 1/2 在任何有 GPU 的环境应成立（跳过逻辑本身待本地验证）；步骤 3/4 依赖双卡硬件，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `IKey` 要在**构造时**就抓取当前设备，而不是比较时再查？

**答案**：键代表「这次请求在哪个设备上发生」。若比较时才查设备，两个线程/两个时刻的查询可能落在不同设备上，等值判断就不稳定了；构造时抓取把设备固化进键对象，哈希与相等比较都基于同一份快照，逻辑确定且线程安全（[python/mod_cvcuda/include/nvcv/python/Cache.hpp:L34-L45](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/Cache.hpp#L34-L45)）。

**练习 2**：`with stream:` 在设备不匹配时抛 `StreamError`，但只对 `m_owns` 为真的流检查（[python/mod_cvcuda/nvcv/Stream.cpp:L487-L501](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L487-L501)）。为什么包装外部流（`as_stream` 得到的）不做这个检查？

**答案**：自建的流（`m_owns == true`）的销毁会回到创建设备上做 `cudaStreamDestroy`（`destroy()` 里的保存/切换/恢复逻辑，[python/mod_cvcuda/nvcv/Stream.cpp:L369-L380](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L369-L380)），激活设备与创建设备不一致容易踩 CUDA「跨设备操作流」的坑，提前拦下最稳。包装的外部流不归 cvcuda 所有、也没有可靠的创建设备信息，流本身的生命周期由原框架负责，强行校验反而会误伤合法用法（比如 torch 在设备 1 上造的流配合正确的当前设备使用）。

**练习 3**：为什么「算子持久缓冲按设备分配」需要专门测试？举一个会翻车的场景。

**答案**：算子对象（如 gaussian 的 `m_kernel`）在第一次 submit 时按当时的当前设备分配缓冲并缓存；如果实现里缓存不区分设备，第二次在另一张卡上用同一算子对象时，kernel 会去解引用**设备 0 上的指针** —— 在设备 1 的上下文里这是非法地址，得到 `cudaErrorIllegalAddress` 这类异步错误，极难定位。所以官方逐个点名高危算子在两卡上顺序执行（[tests/cvcuda/python/test_multi_gpu.py:L293-L308](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_gpu.py#L293-L308) 及其后续用例）作为回归防线。这也是 u8 系列讲算子实现时要留意的点：**设备相关的状态要按设备键控**。

## 5. 综合实践

把本讲三条主线（多流、多线程、共享资源的安全性）串成一个任务：**4 线程并发 flip 管线 + 故意复现一次竞争**。

### 5.1 任务说明

- Part A（正确版）：4 个线程，每线程独立创建 `cvcuda.Stream`、独立输入/输出张量，循环执行 `flip_into`，结束后在主线程校验全部输出正确。
- Part B（错误版）：仍 4 线程，但**共享同一个输出张量**，其余不变 —— 观察结果如何被竞争破坏。
- Part C：对照 `Stream.cpp` 的 `holdResources`，解释为什么 Part B 里「输出张量没被提前释放」不是问题，而「写入顺序」才是问题。

### 5.2 参考实现（示例代码）

```python
# threaded_flip.py —— 示例代码
import threading
import numpy as np
import cupy
import cvcuda

H, W, C = 512, 512, 3
NB_THREADS, LOOPS = 4, 300

def make_src(no):
    rng = np.random.default_rng(no)
    src_np = rng.integers(0, 256, (H, W, C), dtype=np.uint8)
    return src_np, cvcuda.as_tensor(cupy.asarray(src_np), "HWC")

def worker_correct(no, barrier, results):
    stream = cvcuda.Stream()
    src_np, src = make_src(no)
    dst = cvcuda.Tensor((H, W, C), dtype=cvcuda.Type.U8, layout="HWC")
    barrier.wait()
    for _ in range(LOOPS):
        cvcuda.flip_into(dst, src, 0, stream=stream)   # flipCode=0: 上下翻
    stream.sync()
    got = cupy.asarray(dst.cuda()).get()
    results[no] = np.array_equal(got, np.flip(src_np, axis=0))

def worker_racy(no, barrier, shared_dst, results):
    stream = cvcuda.Stream()
    src_np, src = make_src(no)
    barrier.wait()
    for _ in range(LOOPS):
        cvcuda.flip_into(shared_dst, src, 0, stream=stream)  # 故意共享输出！
    stream.sync()
    results[no] = cupy.asarray(shared_dst.cuda()).get()

def run(worker_fn, *extra):
    barrier = threading.Barrier(NB_THREADS)
    results = [None] * NB_THREADS
    threads = [threading.Thread(target=worker_fn, args=(i, barrier, results, *extra))
               for i in range(NB_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results

if __name__ == "__main__":
    # Part A: 每线程独立资源
    ok = run(worker_correct)
    print("Part A (独立输出):", ok)          # 期望 [True]*4

    # Part B: 共享同一输出张量
    shared = cvcuda.Tensor((H, W, C), dtype=cvcuda.Type.U8, layout="HWC")
    outs = run(worker_racy, shared)
    with threading.Lock():                    # 只保护打印整理，不改变竞争本身
        agree = all(np.array_equal(outs[0], o) for o in outs[1:]) if outs[0] is not None else None
        print("Part B (共享输出) 各线程读到的终态一致:", agree)
```

### 5.3 观察与记录

1. Part A 每线程应为 True —— 印证「每线程独立流 + 独立资源 + 显式 `stream=`」是官方同款安全模式。
2. Part B 中四条流没有任何相互依赖地写同一块显存：终态由各流 kernel 的完成顺序决定，`outs` 里四份读取可能不一致（谁后 sync 谁读到的新可能更多）；即便偶尔一致，也只是竞争没被触发，不是正确。
3. Part C 的解释要点：输出张量的**生命周期**由守卫的 hold 机制兜底（辅助流回调保活，见 4.3.3 第 (5) 点），所以不会出现「显存被提前释放」的崩溃；但**写入次序**是用户组织的流依赖问题，库不插手 —— 两条流写同一缓冲，必须由你自己加 `wait_stream` 串行化，或者干脆每流一个缓冲回到 Part A。
4. 性能附加题（可选）：把 Part A 与「单线程同总工作量」比吞吐，多线程提交是否带来收益？记录数字并解释（提示：GIL 串行化的是 Python 侧提交路径，kernel 本身并行；小 kernel 高频提交时瓶颈可能在提交侧）。具体数值**待本地验证**。

## 6. 本讲小结

- **多流**：`wait_stream` 用「对方流记录事件 + 本流等待事件」建立只覆盖**已入队工作**的依赖；链式与扇出是两种基本组网，官方范式在 `test_multi_stream.py`，自等待被显式短路以免死锁。
- **流栈真相**：`StreamStack` 是进程级 Meyers 单例 + 互斥锁，`with`/`Stream.current` 只保证内存安全、不保证线程隔离 —— 多线程代码必须显式传 `stream=`。
- **多线程**：对象缓存表 `thread_local`（每线程一份、显存随线程数放大），配额账本与锁全局共享；官方 `run_parallel` 用屏障 + 异常转发压测并行调用，`test_parallel_bndbox` 守护着「全局缓存在 nogil 下崩溃」的历史教训不再复发。
- **锁模式**：每个 `Resource` 一把互斥锁保护「上次流/设备」记账，`submitSync` 五分支判定 —— 同流快路径零 CUDA 调用（稳态开销极低的根源）、首次绑定、哨兵全同步、跨设备流同步、同设备事件等待；hold 半边经辅助流回调延长资源生命周期。
- **多 GPU**：分配跟随当前设备、缓存键揉入设备号（跨卡绝不复用）、事件不能跨设备（退化为重量级同步）、`with` 前校验流的创建设备、算子持久缓冲必须按设备重分配。
- **总原则**：库替你管「跨流/跨库的隐式同步」与「资源生命周期」；「流依赖的显式组织」与「输出缓冲的独占」始终是调用者的责任。

## 7. 下一步学习建议

本讲结束第四单元（执行模型），下一单元进入**算子内部解剖**：

1. 下一讲 [u5-l1]「算子四层结构」：从 `python/mod_cvcuda/operators/OpFlip.cpp` 出发，跟踪一次调用穿过 C API、C++ 类、priv 实现到 legacy kernel —— 本讲反复出现的 `guard.run(...)` 里的 `Flip->submit(...)` 正是那条链的入口。
2. 带着本讲的问题读源码会更有效：priv 层里哪些算子持有持久设备缓冲（多 GPU 测试点名的五个只是起点）？它们如何按键控？这直接衔接 [u5-l3] 两种内核形态。
3. 并发性能调优的实操（NVTX 时间线、找串行瓶颈）在 [u7-l4] 展开；到时可用本讲 Part A/B 的脚本作为被剖析对象。
4. 若你关心 python 绑定层的资源守卫如何被 C API 承载（`Resources_SubmitSyncOnly` 等），可在进入 u5 前顺带浏览 `python/mod_cvcuda/nvcv/CAPI.hpp`，它把本讲的锁模式暴露成了跨编译单元的稳定边界。

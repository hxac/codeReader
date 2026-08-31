# Stream 执行模型：一切算子都提交到流上

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `cvcuda.Stream` Python 对象与底层 `cudaStream_t` 的对应关系，说清 `cvcuda.Stream()`、`cvcuda.Stream.current`、`cvcuda.Stream.default` 三者分别是什么。
2. 说明算子的 `stream=` 参数如何一路传递：Python 绑定 → `Flip->submit()` → C API `cvcudaFlipSubmit` → priv 实现 → legacy kernel 的 `<<<grid, block, 0, stream>>>` 第四个启动参数。
3. 理解 C++ 侧的 `nvcv::util::CudaStream` RAII 封装，以及它与 Python 侧 Stream 的分工差异。
4. 掌握跨流协作的正确姿势：`wait_stream`、CV-CUDA 自动插入的 `cudaStreamWaitEvent`（接住 u2-l4 埋下的 `seedLastStream` 伏笔），以及为什么「忘了同步」在 CV-CUDA 中往往不会立刻出错、却可能在换流后酿成数据竞争。

本讲是第四单元「执行模型」的第一讲。前三单元我们只用了「当前流」的默认行为；从本讲起，我们显式地控制算子跑在哪条流上。

## 2. 前置知识

### 2.1 CUDA 流是什么

把 GPU 想象成一个有多个窗口的银行，**流（stream）**就是每个窗口前的队列：

- CPU 上的函数调用（如 `cvcuda.flip(...)`）并不等 GPU 干完活，它只是把一个「任务」**异步提交**到某条流的队尾，然后立刻返回。这叫异步执行模型：CPU 是提交者，GPU 是执行者。
- 同一条流内的任务严格按提交顺序执行（FIFO，先进先出）。
- **不同流之间的任务没有顺序保证**——它们可能并行执行，也可能交错执行，这正是多流提速的来源，也是数据竞争的来源。
- `cudaStreamSynchronize(stream)` 让 CPU 阻塞等待某条流上已提交的全部任务完成；`cudaEventRecord` + `cudaStreamWaitEvent` 则是 GPU 侧的「流 A 等流 B」依赖原语。

### 2.2 默认流与非阻塞流

- CUDA 的 0 号流又称 legacy default stream（句柄为 `nullptr`）。它的特殊之处是**隐式同步**：默认流上的操作会等待其他所有阻塞式流完成，也会阻塞它们。
- 用 `cudaStreamCreateWithFlags(&s, cudaStreamNonBlocking)` 创建的流是**非阻塞流**：它不与默认流互相等待，因此能真正与其他流并发。CV-CUDA 中 `cvcuda.Stream()` 创建的正是非阻塞流。

### 2.3 与前面讲义的衔接

- u3-l3 讲过 allocating 与 `_into` 变体，当时所有算子都提交到「当前流」——本讲揭晓「当前流」由谁决定（每线程的流栈）。
- u2-l4 讲过 `as_tensor` 包装外部数组时会记录生产者流（`seedLastStream`），并预告「cvcuda 会在第一次使用前自动插入等待」——本讲 4.4 节兑现这个承诺。
- u1-l4 提过 ResourceGuard 读写锁：本讲会看到它除了加锁，还承担「在 kernel 之前插入跨流等待」的职责。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [python/mod_cvcuda/nvcv/Stream.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp) | Python `cvcuda.Stream` 的实现体：创建/包装外部流、流栈 Current()、wait_stream、辅助流与资源生命周期 |
| [python/mod_cvcuda/nvcv/Stream.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.hpp) | 上述类的声明（`nvcvpy::priv::Stream`） |
| [python/mod_cvcuda/nvcv/StreamStack.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/StreamStack.hpp) | 每线程「当前流」栈，支撑 `with stream:` 上下文 |
| [python/mod_cvcuda/include/nvcv/python/Stream.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/Stream.hpp) | 算子绑定使用的轻量 `nvcvpy::Stream` 包装（`Stream::Current()` / `cudaHandle()`） |
| [python/mod_cvcuda/operators/OpFlip.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp) | flip 的 Python 绑定：`stream=` 参数入口、`Stream::Current()` 兜底、ResourceGuard |
| [src/cvcuda/include/cvcuda/OpFlip.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.hpp) | C++ 类 `cvcuda::Flip`：`operator()(cudaStream_t, ...)` 内联转调 C API |
| [src/cvcuda/OpFlip.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp) | C API `cvcudaFlipSubmit`：stream 参数穿过 ABI 边界 |
| [src/cvcuda/priv/OpFlip.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.cpp) | priv 实现：exportData 后把 stream 传给 legacy 内核 |
| [src/cvcuda/priv/legacy/flip.cu](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu) | 最终落点：`<<<gridSize, blockSize, 0, stream>>>` |
| [src/cvcuda/util/Stream.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/util/Stream.hpp) / [Stream.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/util/Stream.cpp) | C++ 侧 `CudaStream` RAII 封装 |
| [python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp) | 提交前的跨流同步（sync-before-kernel） |
| [python/mod_cvcuda/nvcv/Resource.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp) | `syncThrough`：同流快路径 / 跨流 event / 跨设备兜底 |
| [tests/cvcuda/python/test_multi_stream.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_stream.py) | 官方多流行为测试，实践任务的参考范本 |

注意命名辨析：仓库里有**三个**名字都叫 Stream 的类，职责不同——

1. `nvcvpy::priv::Stream`（python/mod_cvcuda/nvcv/Stream.hpp）：Python 世界里的流对象，带缓存、流栈、辅助流。
2. `nvcvpy::Stream`（python/mod_cvcuda/include/nvcv/python/Stream.hpp）：pybind11 内部传递用的薄包装，本质是持有 Python 对象的 `py::object`。
3. `nvcv::util::CudaStream`（src/cvcuda/util/Stream.hpp）：C++ 库内部的 RAII 流句柄，Python 用户看不到它。

## 4. 核心概念与源码讲解

### 4.1 Python 侧的 cvcuda.Stream：创建、流栈与 Stream::Current()

#### 4.1.1 概念说明

Python 用户眼中的 `cvcuda.Stream` 是一个可以「提交算子」的执行队列。三个关键事实：

- **`cvcuda.Stream()` 创建非阻塞流**：底层是 `cudaStreamCreateWithFlags(..., cudaStreamNonBlocking)`，因此能与默认流和其他非阻塞流并发。
- **`cvcuda.Stream.default` 包装 legacy 默认流（0 号流）**：模块加载时创建，隐式同步语义。
- **`cvcuda.Stream.current` 是「每线程当前流」**：所有不带 `stream=` 参数的算子调用都提交到它。用 `with stream:` 上下文可以临时切换当前流。

为什么需要「当前流」而不是强制每次传 `stream=`？因为多数脚本只有一条流，逐算子传参太啰嗦；而 `with` 块又给了批量切换的自由。这是「显式优于隐式、但默认要省事」的折中。

#### 4.1.2 核心流程

```text
import cvcuda
  └─ Stream::Export()                       # 模块初始化
       ├─ 包装 0 号流 → cvcuda.Stream.default
       └─ StreamStack.push(default)          # 每线程流栈的栈底

cvcuda.Stream()                              # 用户创建
  └─ Stream::Create()
       ├─ Cache::fetch(Stream::Key{})        # 先查缓存（复用"无人使用"的流）
       └─ 未命中 → new Stream()
            └─ cudaStreamCreateWithFlags(cudaStreamNonBlocking)

with stream:                                 # 上下文管理
  └─ __enter__  → StreamStack.push(stream)   # 当前流 = stream
  └─ __exit__   → StreamStack.pop()          # 恢复外层当前流

算子调用（无 stream= 参数）
  └─ Stream::Current() → StreamStack.top()   # 拿到当前流
```

#### 4.1.3 源码精读

**创建：非阻塞标志 + 缓存复用**

[python/mod_cvcuda/nvcv/Stream.cpp:240-255](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L240-L255) 中私有构造函数直接创建非阻塞 CUDA 流，失败时清理后重抛异常：

```cpp
Stream::Stream()
    : m_owns(true)
    , m_size_inbytes(doComputeSizeInBytes())
{
    try
    {
        util::CheckThrow(cudaStreamCreateWithFlags(&m_handle, cudaStreamNonBlocking));
        incrementInstanceCount();
        GetAuxStream();
    }
    catch (...) { destroy(); throw; }
}
```

而公开入口是 [Stream::Create()，python/mod_cvcuda/nvcv/Stream.cpp:221-238](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L221-L238)：它先按 `Stream::Key{}` 查对象缓存，命中就返回缓存中的流。注意 [Stream.cpp:211-219](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L211-L219) 中 Key 的哈希恒为 0、兼容性恒为真——**在缓存看来所有流都等价**，任何一条空闲流都能被复用。正因如此，官方测试断言了两个「仍在使用」的流对象必须不同（[tests/cvcuda/python/test_multi_stream.py:24-27](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_stream.py#L24-L27) `assert stream1 is not stream2`）：被 Python 变量引用着的流处于 in-use 状态，不会被缓存交出去。缓存的完整机制是下一讲（u4-l2）的主题。

**默认流与当前流**

模块导出时，[python/mod_cvcuda/nvcv/Stream.cpp:832-850](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L832-L850) 把 CUDA legacy 默认流（`nullptr` 句柄）包成 `ExternalStream`，构造出 `cvcuda.Stream.default`，并压入流栈作为栈底：

```cpp
static priv::ExternalStream<priv::VOIDP> cudaDefaultStream(static_cast<cudaStream_t>(nullptr));
globalStream = std::make_shared<Stream>(cudaDefaultStream);
StreamStack::Instance().push(*globalStream);
stream.attr("default") = globalStream;
```

同一段代码还注册了 `cvcuda.Stream.current` 静态属性（[Stream.cpp:842-844](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L842-L844)），它转调 [Stream::Current()，python/mod_cvcuda/nvcv/Stream.cpp:472-485](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L472-L485)——取流栈顶：

```cpp
Stream &Stream::Current()
{
    auto defStream = StreamStack::Instance().top();
    if (!defStream)
        throw StreamError("No default cvcuda.Stream available ...");
    return *defStream;
}
```

流栈本身在 [python/mod_cvcuda/nvcv/StreamStack.hpp:29-42](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/StreamStack.hpp#L29-L42)，是一个存 `weak_ptr` 的 `std::stack` 加互斥锁，**每个线程一个实例**（这就是 u4-l3 多线程讲「每线程独立当前流」的基础）。

**with 上下文：activate/deactivate**

[python/mod_cvcuda/nvcv/Stream.cpp:487-507](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L487-L507)：`__enter__` 先做设备一致性检查（自建流不能跨设备激活），再压栈；`__exit__` 弹栈。于是嵌套 `with` 的行为就是栈式的进出，[tests/cvcuda/python/test_multi_stream.py:46-57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_stream.py#L46-L57) 的嵌套断言验证了这一点。

**外部流包装：torch/cupy/ctypes/整数**

[python/mod_cvcuda/nvcv/Stream.cpp:151-203](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L151-L203) 定义了四个 pybind11 `type_caster`，分别识别 `torch.cuda.Stream`（读 `cuda_stream` 属性）、`cupy.cuda.Stream`（读 `ptr` 属性）、`ctypes.c_void_p` 和整数；配合 [ExportExternalStream，Stream.cpp:683-692](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L683-L692) 生成四个重载的 `cvcuda.as_stream()`。包装构造函数 [Stream.cpp:257-276](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L257-L276) 用 `cudaStreamGetFlags` 校验句柄合法性，且 `m_owns` 保持 false——**包装的流不归 CV-CUDA 所有，析构时不会销毁它**。

#### 4.1.4 代码实践

1. **实践目标**：验证 `Stream.current` / `Stream.default` / `with` 上下文的关系。
2. **操作步骤**：运行下面的脚本（示例代码，需已安装 cvcuda wheel 且有 GPU）：

```python
# 示例代码
import cvcuda

print("默认当前流 is default:", cvcuda.Stream.current is cvcuda.Stream.default)
print("default 的句柄:", cvcuda.Stream.default.handle)   # 0 号流，句柄为 0

s1, s2 = cvcuda.Stream(), cvcuda.Stream()
print("两个新建流对象不同:", s1 is not s2)
print("句柄分别是:", hex(s1.handle), hex(s2.handle))

with s1:
    print("进入 s1 后当前流 is s1:", cvcuda.Stream.current is s1)
    with s2:
        print("嵌套 s2 后当前流 is s2:", cvcuda.Stream.current is s2)
    print("退出 s2 后当前流 is s1:", cvcuda.Stream.current is s1)
print("全部退出后回到 default:", cvcuda.Stream.current is cvcuda.Stream.default)
```

3. **需要观察的现象**：句柄值各不相同；`default.handle` 为 0；嵌套进出严格按栈序。
4. **预期结果**：全部打印 `True`，与 [tests/cvcuda/python/test_multi_stream.py:36-57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_multi_stream.py#L36-L57) 的断言一致。
5. 本环境无 GPU，运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `cvcuda.Stream()` 创建的流要与默认流「非阻塞」？如果用阻塞式流会失去什么？
**答案**：非阻塞流不与 legacy 默认流互相隐式同步，因此能与默认流及其他流真正并发；若用阻塞式流，任何一次提交都会等到所有阻塞式流清空，多流并发就退化成串行，多流优化失效。

**练习 2**：`cvcuda.Stream.current` 在两个 Python 线程里同时取，会拿到同一个流吗？
**答案**：不一定。`Current()` 读的是**每线程**的 `StreamStack`（StreamStack.hpp:29-42），线程 A `with s1:` 后它的当前流是 s1，线程 B 仍是自己的栈顶（初始为 default）。所以各线程独立、互不串扰，这是多线程安全的基础（详见 u4-l3）。

### 4.2 stream 参数的完整传递链：从 `flip(stream=...)` 到 kernel 启动

#### 4.2.1 概念说明

每个 Python 算子都有一个仅限关键字的 `stream=` 参数，缺省 `None`。CV-CUDA 的规矩是：**stream 一旦确定，就原封不动地穿透所有层，直到成为 kernel 启动的第四个配置参数**。中途没有任何一层会偷换或额外同步。理解这条链，就理解了「算子跑在哪条流上」这个问题的全部答案。

#### 4.2.2 核心流程

以 `cvcuda.flip_into(dst, src, 1, stream=s)` 为例（allocating 版只是多一步创建输出张量，u3-l3 已讲）：

```text
Python:  cvcuda.flip_into(dst, src, 1, stream=s)
  │
  ① python/mod_cvcuda/operators/OpFlip.cpp : FlipInto
  │     pstream 为空 → Stream::Current() 兜底
  │     ResourceGuard guard(*pstream)      ← 4.4 节展开
  │     Flip->submit(pstream->cudaHandle(), ...)
  │        └─ nvcvpy::Stream::cudaHandle() 经 C API 取回裸 cudaStream_t
  ② src/cvcuda/include/cvcuda/OpFlip.hpp : Flip::operator()
  │     内联转调 cvcudaFlipSubmit(m_handle, stream, ...)
  ③ src/cvcuda/OpFlip.cpp : cvcudaFlipSubmit（C ABI 边界）
  │     ProtectCall 内 ToDynamicRef<Flip>(handle)(stream, ...)
  ④ src/cvcuda/priv/OpFlip.cpp : Flip::operator()
  │     exportData 拿到 GPU 数据视图
  │     m_legacyOp->infer(*input, *output, flipCode, stream)
  ⑤ src/cvcuda/priv/legacy/flip.cu : runFlipKernel
  │     flipVertical<...><<<gridSize, blockSize, 0, stream>>>(...)
  ▼
  kernel 进入 stream 对应的 GPU 队列
```

#### 4.2.3 源码精读

**① 绑定层：缺省兜底 + 句柄取回**

[python/mod_cvcuda/operators/OpFlip.cpp:35-53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L35-L53) 是 `flip_into` 的 Tensor 版实现：

```cpp
Tensor FlipInto(Tensor &output, Tensor &input, int32_t flipCode, std::optional<Stream> pstream)
{
    if (!pstream)
    {
        pstream = Stream::Current();          // 未传 stream= → 每线程当前流
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

四个要点：

- `std::optional<Stream> pstream` 对应 Python 的 `stream=None` 缺省；空则取 `Stream::Current()`。
- `pstream->cudaHandle()`：这里的 `Stream` 是 [python/mod_cvcuda/include/nvcv/python/Stream.hpp:43-48](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/Stream.hpp#L43-L48) 的薄包装，`cudaHandle()` 经内部 C API 表（[python/mod_cvcuda/nvcv/CAPI.cpp:511](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/CAPI.cpp#L511) `ImplStream_GetCudaHandle`）把 Python 对象变回裸 `cudaStream_t`。
- `ResourceGuard` 的职责在 4.4 节展开；此处只需知道 `guard.run(...)` 包住了 submit。
- `submit` 调用把流句柄作为**第一个参数**传入。

注册处 [python/mod_cvcuda/operators/OpFlip.cpp:96](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L96) 声明了 `py::kw_only(), "stream"_a = nullptr`——`stream` 是仅限关键字参数，这是全库统一约定。

**② C++ 类层：一行转调**

[OpFlip.hpp:66-70](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.hpp#L66-L70)（注意是 `src/cvcuda/include/cvcuda/` 下的公开头）：

```cpp
inline void Flip::operator()(cudaStream_t stream, const nvcv::Tensor &in, const nvcv::Tensor &out,
                             int32_t flipCode) const
{
    nvcv::detail::CheckThrow(cvcudaFlipSubmit(m_handle.get(), stream, in.handle(), out.handle(), flipCode));
}
```

C++ 类只持有一个不透明句柄 `m_handle`，把 stream 连同张量句柄一起交给 C API。

**③ C API 层：跨 ABI 边界**

[src/cvcuda/OpFlip.cpp:45-56](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp#L45-L56)：

```cpp
CVCUDA_DEFINE_API(0, 2, NVCVStatus, cvcudaFlipSubmit,
                  (NVCVOperatorHandle handle, cudaStream_t stream, NVCVTensorHandle in, NVCVTensorHandle out,
                   int32_t flipCode))
{
    CVCUDA_NVTX_RANGE("cvcudaFlipSubmit");
    return nvcv::ProtectCall(
        [&out, &in, &handle, &stream, &flipCode]
        {
            nvcv::TensorWrapHandle output(out);
            nvcv::TensorWrapHandle input(in);
            priv::ToDynamicRef<priv::Flip>(handle)(stream, input.resource(), output.resource(), flipCode);
        });
}
```

`cvcudaFlipSubmit` 是动态库导出的纯 C 符号（符号版本机制见 u6-l2），`cudaStream_t stream` 就是一个普通指针参数穿过 ABI。这里还顺手打了一个 NVTX 埋点（u7-l4 会用到）。

**④ priv 实现层：校验后连同 stream 交给内核**

[src/cvcuda/priv/OpFlip.cpp:39-57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.cpp#L39-L57)：先 `exportData` 校验张量是 CUDA 可访问的 strided 数据（u5-l2 主题），再调 legacy 算子的 `infer(..., stream)`。变长批版本 [priv/OpFlip.cpp:59-87](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/OpFlip.cpp#L59-L87) 同样以 stream 收尾。

**⑤ kernel 层：stream 成为启动配置的第四参数**

[src/cvcuda/priv/legacy/flip.cu:130-151](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L130-L151)：

```cpp
template<int NIX, typename SrcWrap, typename DstWrap>
void runFlipKernel(SrcWrap src, DstWrap dst, Size2D dstSize, int numSamples, int32_t flipCode, cudaStream_t stream)
{
    dim3 blockSize(32, 8, 1);
    dim3 gridSize(divUp(divUp(dstSize.w, NIX), blockSize.x), divUp(dstSize.h, blockSize.y), numSamples);
    if (flipCode > 0)
        flipHorizontal<NIX><<<gridSize, blockSize, 0, stream>>>(src, dst, dstSize);
    ...
}
```

`<<<gridSize, blockSize, 0, stream>>>` 的三执行配置参数依次是 grid、block、共享内存字节数，第四个就是流。到这一步，你在 Python 里传的 `stream=s` 与 GPU 队列里的 kernel 一一对应。函数内没有任何 `cudaStreamSynchronize`/`cudaDeviceSynchronize`（只有 [flip.cu:180-183](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/legacy/flip.cu#L180-L183) 在 `CUDA_DEBUG_LOG` 调试宏下才同步）——**算子始终保持异步**，等不等你说了算。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：把 4.2.2 的五步链路在源码中亲手走一遍，并验证「显式传 stream」与「with 上下文」两条路等价。
2. **操作步骤**：
   - 用 4.2.3 的五个链接依次打开文件，确认每一处签名里 stream 的位置，抄成一张「stream 旅行卡片」。
   - 再运行下面的对照脚本（示例代码）：

```python
# 示例代码
import numpy as np
import cupy as cp
import cvcuda

# 构造一张 HWC uint8 张量（cupy 数组经 as_tensor 零拷贝纳管，见 u2-l4）
src = cvcuda.as_tensor(cp.random.randint(0, 255, (240, 320, 3), dtype=np.uint8), "HWC")
dst = cvcuda.Tensor((240, 320, 3), np.uint8, layout="HWC")   # 预分配输出

s = cvcuda.Stream()
# 写法 A：显式传 stream=
cvcuda.flip_into(dst, src, 1, stream=s)
s.sync()

# 写法 B：with 上下文切换当前流
with s:
    cvcuda.flip_into(dst, src, 1)      # stream= 缺省 → Current() == s
s.sync()
```

   - 两种写法各跑一次，在 [OpFlip.cpp:37-40](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L37-L40) 处设断点（或用 u7-l4 的 NVTX 时间线）观察 `pstream` 是否都解析为 `s`。
3. **需要观察的现象**：两种写法落到同一条流；NVTX 里两次 `cvcuda.flip_into` 区间都在 `s` 上。
4. **预期结果**：行为完全一致——`with` 只是改写 `Current()` 的返回值，链路其余部分不变。
5. 运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `stream=s` 传给 allocating 版 `cvcuda.flip(src, 1, stream=s)`，输出张量的创建（`Tensor::Create`，发生在 CPU 侧）也在流 `s` 上吗？
**答案**：不在。看 [OpFlip.cpp:55-60](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L55-L60)：`Flip` 先 `Tensor::Create` 再委托 `FlipInto`，输出张量的显存分配是 CPU 侧的同步动作（可能伴随 `cudaMalloc`），只有随后的算子 kernel 才提交到 `s`。

**练习 2**：为什么 CV-CUDA 坚持算子内部绝不同步流，把同步权留给用户？
**答案**：同步是全局屏障，库内部擅自同步会摧毁多流并发（一条流 sync 等于把并行机会浪费掉），也破坏异步流水线（解码-处理-编码各段本可重叠）。把同步权交给用户，库才能在多流场景（4.3/4.4、u4-l3）中保持可组合性。

### 4.3 C++ 侧的 CudaStream RAII 封装

#### 4.3.1 概念说明

C++/C 用户拿到的是裸 `cudaStream_t`，自己 `cudaStreamCreate` / `cudaStreamDestroy`。C++ 侧库内部（以及想写健壮 C++ 管线的用户）可以用 `nvcv::util::CudaStream`：一个基于 CRTP 的 `UniqueHandle` RAII 包装——构造获得句柄，析构自动销毁，异常路径也不泄漏。它与 Python 侧 Stream 的区别是：**没有缓存、没有流栈、没有辅助流**，纯粹管理句柄生命周期。

#### 4.3.2 核心流程

```text
CudaStream::Create(nonBlocking, deviceId=-1)
  ├─ deviceId >= 0 → 暂存当前设备，cudaSetDevice(deviceId)   # 流属于设备！
  ├─ cudaStreamCreateWithFlags(NonBlocking 或 Default)
  ├─ 恢复原设备
  └─ 返回持有句柄的 CudaStream（移动语义）
析构 / reset
  └─ DestroyHandle → cudaStreamDestroy（容忍进程卸载时的错误）
```

关键细节：**CUDA 流是与设备绑定的资源**，在错误设备上创建/销毁流是常见 bug，所以 `Create` 特意做了 save-set-restore 三段式设备切换。

#### 4.3.3 源码精读

类声明在 [src/cvcuda/util/Stream.hpp:40-54](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/util/Stream.hpp#L40-L54)：

```cpp
class CudaStream : public UniqueHandle<cudaStream_t, CudaStream>
{
public:
    NVCV_INHERIT_UNIQUE_HANDLE(cudaStream_t, CudaStream)

    static CudaStream Create(bool nonBlocking, int deviceId = -1);
    static CudaStream CreateWithPriority(bool nonBlocking, int priority, int deviceId = -1);
    static void DestroyHandle(cudaStream_t stream);
};
```

`UniqueHandle` 提供移动构造/赋值与析构时自动调用 `DestroyHandle`（同族封装还有 `CudaEvent` 等，见 u8-l3 的 Event）。实现体 [src/cvcuda/util/Stream.cpp:26-41](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/util/Stream.cpp#L26-L41)：

```cpp
CudaStream CudaStream::Create(bool nonBlocking, int deviceId)
{
    cudaStream_t stream;
    int flags = nonBlocking ? cudaStreamNonBlocking : cudaStreamDefault;
    int prevDev = -1;
    if (deviceId >= 0)
    {
        NVCV_CHECK_THROW(cudaGetDevice(&prevDev));
        NVCV_CHECK_THROW(cudaSetDevice(deviceId));
    }
    auto err = cudaStreamCreateWithFlags(&stream, flags);
    if (prevDev >= 0)
        NVCV_CHECK_THROW(cudaSetDevice(prevDev));   // 恢复原设备
    NVCV_CHECK_THROW(err);
    return CudaStream(stream);
}
```

销毁 [src/cvcuda/util/Stream.cpp:60-67](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/util/Stream.cpp#L60-L67) 有一个值得学习的细节——静默容忍 `cudaErrorCudartUnloading`：

```cpp
void CudaStream::DestroyHandle(cudaStream_t stream)
{
    auto err = cudaStreamDestroy(stream);
    if (err != cudaSuccess && err != cudaErrorCudartUnloading)
        NVCV_CHECK_THROW(err);
}
```

进程退出时 CUDA 运行时可能已在卸载，此时销毁报错是良性噪音，不该在析构路径上抛异常。

对比记忆：Python 侧 `Stream::destroy()`（[python/mod_cvcuda/nvcv/Stream.cpp:369-409](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L369-L409)）比这复杂得多——它还要同步本流、销毁辅助流与事件，因为 Python 对象背后挂着资源生命周期管理；而 C++ 侧 `CudaStream` 只管句柄。**「复杂度长在哪里」取决于对象背了多少责任**，这是两个 Stream 类最好的对照实验。

#### 4.3.4 代码实践（源码阅读型 + 可选上机）

1. **实践目标**：用 C++ RAII 流替换裸句柄管理，体会异常安全。
2. **操作步骤**：阅读下面等价对照（示例代码）：

```cpp
// 示例代码：裸句柄版（异常路径会泄漏）
cudaStream_t raw = nullptr;
cudaStreamCreateWithFlags(&raw, cudaStreamNonBlocking);
op(raw);            // 若这里抛异常，下面的 destroy 不会执行
cudaStreamDestroy(raw);

// 示例代码：RAII 版
nvcv::util::CudaStream s = nvcv::util::CudaStream::Create(/*nonBlocking=*/true);
op(s.get());        // 任意路径退出都自动 cudaStreamDestroy
```

   若已在 u1-l3 构建过仓库，可把片段编进一个小测试程序验证（头文件为 `nvcv/util/Stream.hpp`，链接 nvcv 库）。
3. **需要观察的现象**：裸句柄版在 `op` 抛异常时句柄泄漏；RAII 版不会。
4. **预期结果**：RAII 版在任何退出路径都正确销毁流；进程退出阶段不因卸载中的销毁报错崩溃。
5. 运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`CudaStream::Create(true, 1)` 在设备 1 上创建流之后，调用方的当前设备是几号？
**答案**：仍是调用前的那台。函数先 `cudaGetDevice` 暂存，`cudaSetDevice(1)` 创建流，再 `cudaSetDevice(prevDev)` 恢复——save-set-restore，不留副作用。

**练习 2**：为什么 `DestroyHandle` 要特判 `cudaErrorCudartUnloading`？
**答案**：静态存储期对象的析构可能发生在 CUDA 运行时卸载过程中，此时 `cudaStreamDestroy` 返回该错误属于良性情形；若照常 `NVCV_CHECK_THROW`，析构函数路径上的异常会直接终止进程（析构中抛异常是未定义/危险行为）。

### 4.4 跨流协作：wait_stream 与自动插入的同步

#### 4.4.1 概念说明

多流是双刃剑：流内有序、流间无序。当**流 B 要读流 A 刚写的数据**时，必须显式建立依赖，否则读到旧数据。CV-CUDA 在这里提供三层保护：

1. **手工层**：`stream_b.wait_stream(stream_a)`——「本流等待他流已入队的工作完成」，实现是经典的 `cudaEventRecord` + `cudaStreamWaitEvent`。
2. **自动层（Resource 记账）**：每个张量/图像批记住「最后一次被写在哪条流」（u2-l4 的 `seedLastStream` 伏笔）；算子提交时 `ResourceGuard` 发现资源上次在别的流上写过，就**在该算子 kernel 之前**自动插入 event 等待。
3. **协议层（CAI stream 字段）**：与 cupy/torch 等外部框架交换 buffer 时，通过 `__cuda_array_interface__` 的 `stream` 字段告知生产者流，cvcuda 据此决定是否需要等待。

理解第 2 层最重要：**这就是为什么「把单流脚本直接拆到两条流」通常不立刻出错，但语义上依赖了自动同步；当你绕过 cvcuda（比如直接用 cupy 读）时就失去保护**。

#### 4.4.2 核心流程

```text
cvcuda.flip_into(dst, src, 1, stream=sB)      # dst 上次写在 sA（假设）
  │
  ├─ ResourceGuard::run(fn)
  │    ① capi().Resources_SubmitSyncOnly(...)   # kernel 之前！
  │         └─ 对每个被锁资源 Resource::syncThrough(sB)
  │              ├─ 快路径：上次流 == sB → 直接返回（流内天然有序）
  │              ├─ 跨流：cudaEventRecord(evt, sA) + cudaStreamWaitEvent(sB, evt)
  │              ├─ 跨设备：切设备 + cudaStreamSynchronize(sA)（event 不能跨设备）
  │              └─ 默认流哨兵(1/2) 或未知生产者流：cudaDeviceSynchronize 兜底
  │    ② fn() → Flip->submit(sB, ...)           # 现在 kernel 才入队，必然看到新数据
  │    ③ capi().Stream_HoldResources(...)       # kernel 之后：延长资源生命周期
  ▼
```

时间轴上：等待永远插在 kernel **之前**——`cudaStreamWaitEvent` 只约束「排在其后」的命令。

#### 4.4.3 源码精读

**手工层：wait_stream**

[python/mod_cvcuda/nvcv/Stream.cpp:457-470](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L457-L470)：

```cpp
void Stream::wait_stream(std::shared_ptr<Stream> other)
{
    if (!other) throw std::invalid_argument("other is null");
    if (other->handle() == m_handle) return;          // 自等自是 no-op

    py::gil_scoped_release release;
    cudaEvent_t evt = other->getEvent();
    util::CheckThrow(cudaEventRecord(evt, other->handle()));   // 在对方流上记事件
    util::CheckThrow(cudaStreamWaitEvent(m_handle, evt, 0));   // 本流等待该事件
}
```

事件按设备缓存在 `m_events`（[Stream.cpp:520-541](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L520-L541)，`cudaEventDisableTiming` 的纯同步事件，比计时事件便宜）。导出的 docstring 明确说明：等待的只是「other 当前已入队」的工作，之后 other 再入队的任务不受约束；且不支持跨设备。

**自动层之一：ResourceGuard 的 sync-before-kernel**

[python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp:101-126](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp#L101-L126) 的 `run()`：

```cpp
template<class F>
void run(F &&fn)
{
    capi().Resources_SubmitSyncOnly(m_pyStream.ptr(), m_resourcesPerLockMode.ptr()); // ① 先同步
    ...
    std::forward<F>(fn)();          // ② 再提交 kernel
    commit();                       // ③ 持有资源直到流上工作完成
}
```

头文件注释（[ResourceGuard.hpp:80-100](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/include/nvcv/python/ResourceGuard.hpp#L80-L100)）解释了为什么顺序不能反：`cudaStreamWaitEvent` 只对**排在它之后**的命令生效，先提交 kernel 再插等待等于没保护。步骤③的「持有」通过 [Stream::holdResources，Stream.cpp:543-611](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L543-L611) 把资源闭包挂到辅助流回调上，等流跑完再释放——其中那条著名的双时间轴注释（主流跑 kernel、辅助流跑 CPU 回调，互不阻塞 GPU）值得细读，机制与 u8-l3 的 Workspace 缓存同源。

**自动层之二：Resource::syncThrough 的四分支**

[python/mod_cvcuda/nvcv/Resource.cpp:91-104](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L91-L104) 是最常见的快路径——同一流再次使用，直接返回：

```cpp
// Fast path: previously bound to the same stream → no sync work ...
if (prevHandle != nullptr && prevHandle == stream.handle())
{
    return;
}
```

注释里给了量级：快路径只是「互斥锁 + 指针比较」，避免每个资源一次 `cudaGetDevice`，把一次算子调用的固定开销从约 30µs 压到约 5µs——执行模型的细节直接决定 Python 封装的性能上限。

跨流时走 [Resource.cpp:161-167](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L161-L167) 的 event 路径；而 [Resource.cpp:120-150](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L120-L150) 是防御性兜底：当生产者广告的是 CAI 默认流哨兵（1 = legacy，2 = per-thread）或根本没广告流（CAI v2，如老版 PyTorch）时，event 屏障**捕获不到**非阻塞流上的工作，于是退化为 `cudaDeviceSynchronize` 保正确性。这段注释是理解「跨框架互操作为什么有时会莫名变慢」的钥匙。

**协议层：CAI stream 字段**

`cvcuda.Stream` 的类 docstring（[python/mod_cvcuda/nvcv/Stream.cpp:778-811](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L778-L811)）完整陈述了这套约定：输入侧 `as_tensor` 读生产者的 `__cuda_array_interface__["stream"]` 并安排等待；输出侧 `Tensor.cuda()` 把「最后写入流」写回该字段供下游同步；生产者可用 `stream: -1` 声明「我已同步，别等我」。导出方向的实现就是 [python/mod_cvcuda/nvcv/ExternalBuffer.cpp:361-374](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/ExternalBuffer.cpp#L361-L374) 的 `InsertDLPackStreamSync`——同一个 `cudaEventRecord` + `cudaStreamWaitEvent` 模式。

#### 4.4.4 代码实践

1. **实践目标**：验证「跨流读写同一张量时 CV-CUDA 自动插入等待」，以及 `wait_stream` 的手工用法。
2. **操作步骤**：运行以下脚本（示例代码，需 GPU）：

```python
# 示例代码
import time
import numpy as np
import cupy as cp
import cvcuda

src = cvcuda.as_tensor(cp.zeros((1, 1024, 1024, 3), dtype=np.uint8), "HWC")

sA, sB = cvcuda.Stream(), cvcuda.Stream()

# 场景 1：A 流写，B 流读 —— 不需要手工 wait，ResourceGuard 自动同步
out_a = cvcuda.flip(src, 1, stream=sA)                 # 写 out_a 于 sA
out_b = cvcuda.flip(out_a, 1, stream=sB)               # 读 out_a 于 sB：自动等待
sB.sync()                                               # 只需等消费流

# 场景 2：手工依赖 —— B 等 A 已入队的全部工作
sB.wait_stream(sA)                                      # cudaEventRecord + cudaStreamWaitEvent
out_c = cvcuda.flip(out_a, 0, stream=sB)
sB.sync()

print("全部完成，句柄:", hex(sA.handle), hex(sB.handle))
```

3. **需要观察的现象**：场景 1 无报错且结果正确（自动同步生效）；可在 Nsight Systems 时间线（u7-l4）里看到 B 流 kernel 前多出一个小小的 wait 事件。
4. **预期结果**：跨流数据依赖被正确处理；对比实验——若用 cupy 直接在 sB 上读 `out_a`（绕过 cvcuda 算子），就没有这层保护，属于未定义行为。
5. 运行结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`sB.wait_stream(sA)` 之后，sA 再提交的新 kernel 会被 sB 等到吗？
**答案**：不会。`wait_stream` 在调用那一刻于 sA 上记录事件，只约束「当时已入队」的工作（见 [Stream.cpp:863-867](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L863-L867) 的 docstring）。之后再往 sA 提交的任务需要再次 `wait_stream`。这与 PyTorch 的 `cuda.wait_stream` 语义一致。

**练习 2**：Resource 的快路径（同流复用）为什么敢「什么都不做」？
**答案**：CUDA 保证同一条流内命令按序执行；资源上次写在该流上，本次同流的新 kernel 必然排在那次写入之后，天然可见，无需任何同步原语（[Resource.cpp:91-104](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L91-L104)）。

**练习 3**：生产者经 CAI v2（无 stream 字段）交出 buffer 时，cvcuda 为什么退化为全设备同步而不是 event？
**答案**：event 屏障只对「记录事件的那条流」有效；未知生产者流（或默认流哨兵）意味着写入可能落在任意非阻塞流上，event 挂不上正确的流，只能 `cudaDeviceSynchronize` 一刀切保正确（[Resource.cpp:120-150](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Resource.cpp#L120-L150) 注释）。

## 5. 综合实践

把本讲知识串起来：**双流并行 resize + 单流对照计时 + hello_world 显式流改造**。前置条件：已按 u1-l2 安装 cvcuda wheel，且 `samples/` 可访问（依赖 `samples/common.py` 与素材图）。

### 步骤一：双流并行实验（示例代码）

```python
# 示例代码：dual_stream_resize.py —— 放在 samples/ 目录旁运行以便 import common
import time
import numpy as np
import cupy as cp
import cvcuda

N, H, W = 4, 2160, 3840          # 4 张 4K 图，工作量足够大才能看出并发
DST = (H // 2, W // 2, 3)

def make_batch():
    imgs = [cp.random.randint(0, 255, (1, H, W, 3), dtype=np.uint8) for _ in range(N)]
    return [cvcuda.as_tensor(im, "NHWC") for im in imgs]

def run_split_streams(tensors, s1, s2):
    """奇数图走 s1，偶数图走 s2，各自 _into 到预分配输出"""
    outs = [cvcuda.Tensor((1, *DST), np.uint8, layout="NHWC") for _ in range(N)]
    for i, t in enumerate(tensors):
        s = s1 if i % 2 == 0 else s2
        # 注意：resize_into 不接收输出形状，形状由预分配的 dst 决定
        cvcuda.resize_into(outs[i], t, interp=cvcuda.Interp.LINEAR, stream=s)
    s1.sync(); s2.sync()
    return outs

def run_single_stream(tensors):
    outs = [cvcuda.Tensor((1, *DST), np.uint8, layout="NHWC") for _ in range(N)]
    for i, t in enumerate(tensors):
        cvcuda.resize_into(outs[i], t, interp=cvcuda.Interp.LINEAR)
    cvcuda.Stream.current.sync()
    return outs

tensors = make_batch()
s1, s2 = cvcuda.Stream(), cvcuda.Stream()

# 预热（触发对象缓存/算子缓存分配，见 u4-l2/u3-l3）
run_split_streams(tensors, s1, s2)
run_single_stream(tensors)

t0 = time.perf_counter(); a = run_split_streams(tensors, s1, s2); t_split = time.perf_counter() - t0
t0 = time.perf_counter(); b = run_single_stream(tensors);          t_one  = time.perf_counter() - t0

print(f"双流: {t_split*1000:.1f} ms   单流: {t_one*1000:.1f} ms   加速比: {t_one/t_split:.2f}x")
# 正确性抽查：双流与单流结果应逐位一致（同一插值算法）
assert cp.array_equal(cp.asarray(a[0].cuda()), cp.asarray(b[0].cuda()))
```

**观察点**：

1. 双流版本应快于单流版本（理想接近 2x；实际受限于 GPU 带宽与各图工作量是否均衡，1.2x~1.8x 都属正常）。
2. 正确性断言通过：奇偶图互不依赖，无需手工 `wait_stream`。
3. 刻意改错实验：把两条流换成同一条（`s2 = s1`），加速比应掉回 1.0x——证明提速确实来自并发而非缓存。
4. 用 `nsys profile python3 dual_stream_resize.py` 抓时间线，肉眼确认两组 kernel 在两条流上交错（u7-l4 展开）。

### 步骤二：hello_world 显式流改造

打开 [samples/applications/hello_world.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py)，其主流程（[hello_world.py:194-228](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L194-L228)）目前全部提交到默认当前流。做两处改造（改自己拷贝的文件，勿动仓库源码）：

1. 在 resize 段前创建 `stream = cvcuda.Stream()`，为 `cvcuda.resize(...)` 与 `cvcuda.gaussian(...)` 显式加 `stream=stream`（参照 [samples/operators/clahe.py:56-63](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/operators/clahe.py#L56-L63) 的官方显式流写法）；注意 `cvcuda.stack`（合并批次）必须发生在 `stream.sync()` 之后或同样带上 `stream=`，因为 gaussian 读的是 stack 的输出——想清楚 4.4 节的依赖关系再动手。
2. 保存（编码）之前调用 `stream.sync()`，保证 GPU 结果就绪后再回 CPU。

**预期结果**：输出图与原版逐像素一致；`nsys` 时间线上 resize/gaussian 离开默认流、落到你创建的流区间内。本环境无 GPU，以上**待本地验证**。

## 6. 本讲小结

- `cvcuda.Stream()` 创建的是**非阻塞** CUDA 流（`cudaStreamNonBlocking`），能与默认流并发；`cvcuda.Stream.default` 包装 legacy 0 号流；`cvcuda.Stream.current` 来自**每线程**的 StreamStack，`with stream:` 压栈/弹栈切换。
- 算子的 `stream=` 参数缺省时兜底到 `Stream::Current()`；随后**原样穿透**绑定层（`pstream->cudaHandle()`）→ C++ 类 → C API `cvcudaFlipSubmit` → priv 实现 → legacy kernel 的 `<<<grid, block, 0, stream>>>`，中途任何一层都不偷换、不同步。
- 内核层绝不主动 `cudaStreamSynchronize`（仅调试宏下例外），异步性与同步权完全交给用户——这是多流可组合性的前提。
- C++ 侧 `nvcv::util::CudaStream` 是纯 RAII 句柄（save-set-restore 设备切换、容忍卸载期销毁错误）；Python 侧 Stream 在此之上叠加缓存、流栈、辅助流与资源生命周期。
- 跨流数据安全有三层：手工 `wait_stream`（record+wait，只约束已入队工作）、自动 Resource 记账（ResourceGuard 在 kernel **之前**插 `cudaStreamWaitEvent`；同流走零开销快路径；跨设备/默认流哨兵退化同步）、CAI `stream` 字段协议（与 torch/cupy 互操作）。

## 7. 下一步学习建议

- **u4-l2（Python 对象缓存）**：本讲两次撞见「Stream/Tensor 走缓存」（4.1.3 的 `Stream::Create`、综合实践的预热）。下一讲系统拆解缓存键、命中条件与限额，解释为什么预热后近零分配。
- **u4-l3（多流、多线程与多 GPU）**：本讲的流栈是每线程的，`Resource::syncThrough` 有跨设备分支——多线程/多 GPU 场景的完整语义与官方测试范本在下一讲展开。
- 想先看底层细节的读者：精读 [python/mod_cvcuda/nvcv/Stream.cpp:543-611](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L543-L611) 的辅助流双时间轴设计（主流跑 kernel、辅助流跑 CPU 回调），它与 u8-l3 的 Workspace per-stream 缓存同源。

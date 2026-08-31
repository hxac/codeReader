# u7-l4 NVTX 埋点与性能分析

## 1. 本讲目标

学完本讲,你应该能够:

1. 说出 CV-CUDA 内置 NVTX 埋点体系的完整构成:C++ 核心侧的 `CVCUDA_NVTX_RANGE`、Python 绑定侧的 `NvtxTrace`,以及两层嵌套的时间线结构。
2. 用 Nsight Systems(`nsys profile`)捕获一次 CV-CUDA 程序的运行,并在时间线上找到 `cvcuda.flip` / `cvcudaFlipSubmit` 这样的 NVTX 区间。
3. 理解 NVTX 区间记录的是 **CPU 侧时间**而非 GPU 执行时间,学会把 CPU 调用区间与 GPU kernel 行对齐,从而定位管线中的空闲段与串行瓶颈。
4. 读懂 CV-CUDA 如何用「注入式探针 + 静态源码扫描」两层测试守护埋点不退化——这正是上一讲(u7-l2)「白盒测试」思想在性能可观测性上的延续。

## 2. 前置知识

### 2.1 NVTX 是什么

NVTX(NVIDIA Tools Extension)是 NVIDIA 提供的一个**纯注释库**:它在你的代码里放「路牌」,自己不做任何测量。核心只有一对 C 函数:

- `nvtxRangePushA(name)`:在**当前 CPU 线程**上压入一个命名区间的起点;
- `nvtxRangePop()`:弹出最近的区间,终点即此刻。

两点关键特性:

1. **近零开销**。没有分析器在场时,这两个函数几乎直接返回,对生产路径的干扰可以忽略。这也是 CV-CUDA 敢于在每个算子入口都埋点的原因。
2. **只写不读**。NVTX 把区间交给「消费者」——通常是 Nsight Systems 分析器。NVTX 本身没有任何 API 让你把区间读回来,这一点直接催生了本讲 4.4 节的注入探针设计。

### 2.2 CPU 时间线与 GPU 时间线:初学者最容易混淆的地方

回顾 u4-l1 的结论:CV-CUDA 的一切算子都**异步提交**到 CUDA 流上。Python 调用 `cvcuda.flip(...)` 在 CPU 上只花「参数校验 + kernel 启动」的时间就返回了,GPU 可能还要过一会儿才真正执行这个 kernel。

因此 Nsight Systems 的时间线是**双轨**的:

- 上轨(CPU 线程行):NVTX 区间显示在这里,长度 = `push` 到 `pop` 的墙钟时间,即**提交成本**;
- 下轨(GPU 行):真正的 kernel 执行,长度 = **GPU 计算时间**。

两者靠 CUDA 运行时的 correlation(关联标记)连起来。分析瓶颈时永远要同时看两轨:NVTX 区间帮你回答「这段时间 CPU 在哪个算子的调用里」,GPU 行回答「这段时间显卡在干什么」。**GPU 行上两段 kernel 之间的空隙,就是值得排查的空闲段**。

### 2.3 与前几讲的关系

- u7-l3 的基准测试(bench/)回答「快不快、有没有回归」,是**宏观统计**;本讲的 Nsight Systems 回答「时间具体花在哪一步、谁在等谁」,是**微观定位**。两者互补:基准发现异常,时间线找原因。
- u7-l2 介绍了 `cvcuda.internal` / `cvcuda._test` 白盒测试;本讲 4.4 的 NVTX 注入探针是同一哲学的又一案例——用测试基础设施观测库的内部行为。
- u1-l2 详细剖析过 `hello_world.py` 的六步流水线,本讲综合实践将以它为剖析对象。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/cvcuda/priv/Nvtx.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/Nvtx.hpp) | C++ 核心侧:RAII 的 `Range` 类与 `CVCUDA_NVTX_RANGE` 宏 |
| [python/mod_cvcuda/NvtxRange.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/NvtxRange.hpp) | Python 绑定侧:复制版 `NvtxRange` 类 + `NvtxTrace` 签名保持包装器 |
| [src/cvcuda/OpFlip.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp) | C API 层埋点的使用样本:每个 `*Submit` 函数体第一行 |
| [python/mod_cvcuda/operators/OpFlip.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp) | Python 绑定层埋点的使用样本:每个 `m.def` 都包 `NvtxTrace` |
| [python/mod_cvcuda/nvcv/Stream.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp) | Stream 方法与上下文管理器的埋点(另一种写法:直接声明局部变量) |
| [tests/cvcuda/nvtx_probe/NvtxProbe.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/nvtx_probe/NvtxProbe.cpp) | 测试专用 NVTX 注入库:替换函数表,把区间名记录下来 |
| [tests/cvcuda/python/test_nvtx_markers.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_nvtx_markers.py) | 守护测试:静态源码扫描 + 运行时探针断言 |
| [tests/cvcuda/nvtx_probe/CMakeLists.txt](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/nvtx_probe/CMakeLists.txt) | 探针的构建方式:MODULE 库,只被 dlopen、永不参与链接 |
| [tests/cvcuda/python/cvcuda_test_python.in](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_test_python.in) | Python 测试启动脚本:为何只给 marker 测试单独注入探针 |
| [samples/applications/hello_world.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py) | 综合实践的剖析对象 |

另外说明:大纲里列出的 [docs/sphinx/perf_benchmark.rst](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/docs/sphinx/perf_benchmark.rst#L21-L24) 目前只有一句 "Placeholder for Perf benchmark" 的占位文字——官方性能文档尚未写就,实际可依据的性能工程材料就是 bench 目录(u7-l3)与本讲的源码与测试。

## 4. 核心概念与源码讲解

### 4.1 C++ 核心侧埋点:`CVCUDA_NVTX_RANGE` 宏

#### 4.1.1 概念说明

CV-CUDA 的 C API 层(src/cvcuda/Op*.cpp)是所有算子的公共咽喉:无论调用来自 Python、C 还是 C++,最终都要经过形如 `cvcudaXxxSubmit` 的 extern "C" 函数。在这里埋点,等于给**每一个算子的每一次提交**都挂上统一命名的路牌——分析任何上层应用时,时间线上自动出现细粒度的算子分解,不需要用户自己改代码加标记。

实现上只需要一个极小的 RAII 类加一个宏,总共不到 30 行有效代码。

#### 4.1.2 核心流程

RAII(资源获取即初始化)式的区间管理:

```
函数入口 → 宏展开为创建一个栈上的 Range 对象
        → Range 构造函数调用 nvtxRangePushA(name)   // 区间开始
        → 函数体执行(校验、导出、kernel 启动……)
        → 函数返回 → Range 析构函数调用 nvtxRangePop()  // 区间结束
```

好处是**异常安全**:即使函数体中途抛出 C++ 异常,栈展开时析构函数照样执行,`push`/`pop` 永远配对,时间线不会出现「开始了却没结束」的悬空区间。

宏里还有一个细节:用 `__COUNTER__`(每次展开递增的编译期计数器)拼接变量名,保证同一个作用域里即使写多个 `CVCUDA_NVTX_RANGE` 也不会声明两个同名变量。

#### 4.1.3 源码精读

整个机制的核心定义([src/cvcuda/priv/Nvtx.hpp:25-42](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/Nvtx.hpp#L25-L42))——构造即 push、析构即 pop、拷贝与移动全部禁用(防止对象被复制后双重 pop):

```cpp
class Range final
{
public:
    explicit Range(const char *name) noexcept
    {
        nvtxRangePushA(name);
    }

    ~Range() noexcept
    {
        nvtxRangePop();
    }

    Range(const Range &)            = delete;
    Range(Range &&)                 = delete;
    // ...
};
```

宏定义([src/cvcuda/priv/Nvtx.hpp:46-49](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/Nvtx.hpp#L46-L49)):

```cpp
#define CVCUDA_NVTX_RANGE(name) \
    ::cvcuda::priv::nvtx::Range CVCUDA_NVTX_DETAIL_MAKE_NAME(cvcudaNvtxRange_, __COUNTER__)(name)
```

使用样本——`cvcudaFlipSubmit` 的函数体第一行([src/cvcuda/OpFlip.cpp:45-57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp#L45-L57)):

```cpp
CVCUDA_DEFINE_API(0, 2, NVCVStatus, cvcudaFlipSubmit,
                  (NVCVOperatorHandle handle, cudaStream_t stream, ...))
{
    CVCUDA_NVTX_RANGE("cvcudaFlipSubmit");   // ← 区间名 = C API 函数名
    return nvcv::ProtectCall(
        [&out, &in, &handle, &stream, &flipCode]
        {
            nvcv::TensorWrapHandle output(out);
            nvcv::TensorWrapHandle input(in);
            priv::ToDynamicRef<priv::Flip>(handle)(stream, input.resource(), output.resource(), flipCode);
        });
}
```

三个值得注意的规律:

1. **区间名就是 C API 函数名**(`cvcudaFlipSubmit`),不做任何修饰,这样在时间线上一眼就能对应到头文件里的函数声明。变长批入口同样埋点,名字不同(`cvcudaFlipVarShapeSubmit`,见 [src/cvcuda/OpFlip.cpp:63](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp#L63)),在时间线上可区分两条路径。
2. **`Create` 类函数不埋点**:`cvcudaFlipCreate`([src/cvcuda/OpFlip.cpp:30-43](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp#L30-L43))里没有 `CVCUDA_NVTX_RANGE`。语义上 Create/Destroy 是一次性构造开销(且 Python 侧走对象缓存,几乎不再发生),真正需要按次观测的是 Submit。这也说明埋点是「按价值投放」的,不是无脑铺满。
3. **规模**:用 `rg 'CVCUDA_NVTX_RANGE' src/cvcuda` 统计,当前 HEAD 共 **228 处、分布于 122 个文件**——每个算子的每个 Submit 变体都有,这是全库统一的纪律,而不是个别算子的行为。

埋点的头文件依赖由根 [CMakeLists.txt:114-125](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/CMakeLists.txt#L114-L125) 统一供给:一个名为 `cvcuda_nvtx_config` 的 INTERFACE 库在 CUDA Toolkit 路径下 `find_path` 找 `nvtx3/nvToolsExt.h`,找不到直接 `FATAL_ERROR`。`cvcuda`、`cvcuda_priv`、`cvcuda_legacy` 乃至 4.4 节的探针都链接它——这就是 u1-l3 讲过的「全 find_package、零 FetchContent」策略中 nvtx3 那一项的落点。

#### 4.1.4 代码实践(源码阅读型)

1. **实践目标**:验证「每个 Submit 都埋点、Create 都不埋点」的规律,并学会用检索快速确认任意算子的埋点情况。
2. **操作步骤**:
   - 执行 `rg -c 'CVCUDA_NVTX_RANGE' src/cvcuda | sort -t: -k2 -n | tail` 看埋点最多的文件;
   - 对比 [src/cvcuda/OpGaussian.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpGaussian.cpp) 中 `cvcudaGaussianSubmit` 与 `cvcudaGaussianCreate` 两段函数体;
   - 任选一个你关心的算子(如 `OpResize.cpp`),数一数它有几个 Submit 变体、几个埋点,两者应相等。
3. **需要观察的现象**:每个 `CVCUDA_DEFINE_API(... *Submit ...)` 的函数体第一行都是 `CVCUDA_NVTX_RANGE("...Submit");`,而 `*Create` 函数体里没有。
4. **预期结果**:Submit 数与埋点数一一对应;时间线上因此能区分 Tensor 版与 VarShape 版两条调用路径。

#### 4.1.5 小练习与答案

**练习 1**:如果把 `Range` 类的拷贝构造函数不删除,可能出什么问题?

**答案**:拷贝出来的第二个对象析构时会再调用一次 `nvtxRangePop()`,导致 pop 次数多于 push 次数,NVTX 的区间栈错位,后续所有区间的配对关系全乱,时间线呈现错误嵌套甚至报错。删掉拷贝与移动构造是从类型系统层面杜绝这种错误。

**练习 2**:为什么埋点放在 `Submit` 而不是更深的 priv 实现层(`priv/Flip::operator()`)或 kernel 里?

**答案**:C API 层是所有语言入口(Python/C/C++)的公共咽喉,在这里埋一次覆盖全部调用方;priv 层一个算子可能被多个 Submit 变体共享,埋在那里反而分不清路径。而 kernel 内部不能调 NVTX——`nvtxRangePushA/Pop` 是 CPU 侧 API,运行在设备代码里没有意义。GPU 侧的粒度由 Nsight Systems 自带的 kernel 追踪提供,无需埋点。

### 4.2 Python 绑定侧:`NvtxTrace` 签名保持包装器

#### 4.2.1 概念说明

Python 绑定层(python/mod_cvcuda)也有一份几乎相同的埋点,但目的不同:名字是**用户视角的** `cvcuda.flip`、`cvcuda.flip_into`,并且它包裹的是「整个 Python 调用」——包括输出张量分配(allocating 变体的 `Tensor::Create`、对象缓存查找)、pybind11 参数转换等 Python 侧特有的开销,这些都不经过 C API 的区间。

难点在于:绑定文件里有 61 个算子 × 每个约 4 个重载(Tensor/变长批 × allocating/_into),共 250 多处 `m.def`。如果每个入口都手写「先 push、后 pop」,既啰嗦又容易漏。于是出现了一个精巧的模板包装器 `NvtxTrace`:把 C++ 函数包一层,调用前后自动 push/pop,同时**不改变函数签名**,让 pybind11 完全感觉不到包装的存在。

#### 4.2.2 核心流程

```
m.def("flip", NvtxTrace("cvcuda.flip", &Flip), "src"_a, "flipCode"_a, ...)
              └──────┬──────┘└──┬──┘
                     │          └── 原始函数指针
                     └── 返回一个闭包(lambda):
                          push("cvcuda.flip")
                          result = fn(args...)
                          pop
                          return result
```

关键约束是「签名保持」:pybind11 在 `m.def` 时会对可调用对象做函数签名内省(用于文档、类型转换、参数名与默认值绑定)。`NvtxTrace` 返回的 lambda 显式声明了与原函数相同的参数类型 `(Args... args) -> R`,而非转发引用,所以内省结果与未包装时完全一致;`static_cast<Args&&>(args)...` 则完成了本该由 `std::forward` 做的值类别转换。

#### 4.2.3 源码精读

Python 侧的 `NvtxRange` 类([python/mod_cvcuda/NvtxRange.hpp:29-46](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/NvtxRange.hpp#L29-L46))与核心侧逐行等价。文件注释(第 25-28 行)直接解释了为什么要复制一份:Python 模块**不能 include 核心的私有头文件** `src/cvcuda/priv/Nvtx.hpp`,只好把约 20 行的助手类复制过来。这是分层边界(python 模块只依赖公开头)带来的一次有意识的、被注释说明的小型重复——比引入一条越层依赖更划算。

自由函数版 `NvtxTrace`([python/mod_cvcuda/NvtxRange.hpp:55-67](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/NvtxRange.hpp#L55-L67)):

```cpp
template<typename R, typename... Args>
auto NvtxTrace(const char *name, R (*fn)(Args...))
{
    return [name, fn](Args... args) -> R
    {
        NvtxRange range(name);
        return fn(static_cast<Args &&>(args)...);
    };
}
```

注意注释里的约定:`name` 必须有静态存储期(字符串字面量天然满足),闭包只捕获指针不复制字符串。

成员函数重载([python/mod_cvcuda/NvtxRange.hpp:74-84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/NvtxRange.hpp#L74-L84))服务于 `cls.def(...)` 场景:闭包的第一个参数是实例自身(`const C &self`),这正是 pybind11 把自由可调用对象绑成实例方法的方式,因此包装后的方法签名同样不变。

使用样本——flip 的「四连绑定」([python/mod_cvcuda/operators/OpFlip.cpp:96](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L96)、[L113](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L113)、[L131](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L131)、[L149](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpFlip.cpp#L149)):

```cpp
m.def("flip", NvtxTrace("cvcuda.flip", &Flip), "src"_a, "flipCode"_a,
      py::kw_only(), "stream"_a = nullptr, R"pbdoc(...)pbdoc");
m.def("flip_into", NvtxTrace("cvcuda.flip_into", &FlipInto), ...);
m.def("flip", NvtxTrace("cvcuda.flip", &FlipVarShape), ...);          // 变长批,同名
m.def("flip_into", NvtxTrace("cvcuda.flip_into", &FlipVarShapeInto), ...);
```

命名规则清晰可推:**区间名 = `cvcuda.` + 导出函数名**。同名重载(Tensor 版与变长批版都叫 `flip`)共用同一个区间名——它们本来就无法从 Python 调用点区分,时间线上也无须区分。

另一种等价写法出现在不便用包装器的地方:函数体内直接声明局部变量。Stream 的上下文管理器([python/mod_cvcuda/nvcv/Stream.cpp:487-507](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L487-L507)):

```cpp
void Stream::activate()
{
    ::cvcudapy::NvtxRange nvtxRange("cvcuda.Stream.__enter__");
    // ...压入流栈...
}

void Stream::deactivate(py::object, py::object, py::object) const
{
    ::cvcudapy::NvtxRange nvtxRange("cvcuda.Stream.__exit__");
    StreamStack::Instance().pop();
}
```

同一文件里还有 `cvcuda.Stream.create`([L223](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L223))、`cvcuda.Stream.sync`([L450-455](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L450-L455))、`cvcuda.Stream.wait_stream`([L457-470](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Stream.cpp#L457-L470))。其中 `sync` 的埋点尤其有价值:它包住的是 `cudaStreamSynchronize`,也就是 **CPU 线程的等待时间**——时间线上这段区间有多长,直接说明管线在「等 GPU 干完」上花了多少墙钟时间。

成员函数版 `NvtxTrace` 的样本是 `Tensor.cuda`(DLPack 导出)与容器工厂函数([python/mod_cvcuda/nvcv/Tensor.cpp:540-550](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/nvcv/Tensor.cpp#L540-L550)):`cvcuda.Tensor.cuda`、`cvcuda.as_tensor`、`cvcuda.reshape` 等。这些不是算子,但都是 CPU 侧有感知开销的操作,同样值得在时间线上留下名字。

全库共约 254 处 `NvtxTrace`/`NvtxRange`,分布在 66 个绑定文件中(用 `rg -c 'NvtxTrace\(|NvtxRange ' python/mod_cvcuda` 可复核)。

#### 4.2.4 代码实践(源码阅读型)

1. **实践目标**:为任意一个算子列出它在时间线上会出现的全部 NVTX 区间名,建立「看到区间名 → 知道代码位置」的反射。
2. **操作步骤**:
   - 打开 [python/mod_cvcuda/operators/OpGaussian.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpGaussian.cpp) 与 [src/cvcuda/OpGaussian.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpGaussian.cpp);
   - 把每个 `NvtxTrace("...")` 与每个 `CVCUDA_NVTX_RANGE("...")` 的字符串抄下来,按调用嵌套顺序排成两层列表。
3. **需要观察的现象**:外层是 Python 名(`cvcuda.gaussian` / `cvcuda.gaussian_into`),内层是 C API 名(`cvcudaGaussianSubmit` / `cvcudaGaussianVarShapeSubmit`),一一配对。
4. **预期结果**:得到一张 4×2 的对照表。以后在 nsys 时间线上看到任何一层,都能立刻定位到对应源码文件。

#### 4.2.5 小练习与答案

**练习 1**:`NvtxTrace` 的 lambda 为什么按值声明参数 `(Args... args)` 而不是完美转发 `(Args&&... args)`?

**答案**:pybind11 的函数注册与文档生成依赖对可调用对象 `operator()` 的签名内省。若用转发引用,推导出的签名会变成 `Args&&...`,与原函数签名不一致,参数名绑定(`"src"_a`)、默认值与生成的 docstring 都可能失真。按值声明精确复刻原签名;`static_cast<Args&&>` 再补上值类别转换,兼顾正确性。文件注释(第 61-64 行)明确记录了这一取舍。

**练习 2**:为什么 `cvcuda.Stream.sync` 的 NVTX 区间在性能分析中特别有用?

**答案**:`Stream::sync` 的区间覆盖 `cudaStreamSynchronize`,这是 CPU 线程阻塞等待 GPU 完成的全段时间。在时间线上,该区间长度近似等于「这批工作 GPU 落后的程度」;如果它很长而 GPU 行却是空的,说明 CPU 在干等已经结束的工作或依赖关系排布不当,是典型的可优化信号。

### 4.3 时间线语义:两层嵌套、CPU/GPU 双轨与 nsys 剖析

#### 4.3.1 概念说明

把 4.1、4.2 两节合起来,一次 `cvcuda.flip(tensor, 0)` 在 Nsight Systems 时间线上呈现为**严格嵌套的洋葱结构**:

```
cvcuda.flip                 ← Python 侧区间(含 pybind11 转换、输出分配、对象缓存查找)
└── cvcudaFlipSubmit        ← C API 侧区间(含句柄校验、exportData、kernel 启动)
    └── (GPU 行) FlipKernel ← kernel 实际执行,与上面两轨靠 correlation 关联
```

外层比内层多出的部分,正是「Python 绑定层自身开销」——u7-l3 讲过基准体系里有 C++/Python parity(对齐)检查,当 Python 明显偏慢就怀疑绑定层缺陷;本讲的时间线给出直接证据:两层区间之差就是绑定开销的可视化。

同理,Stream 与容器方法的埋点(`cvcuda.Stream.sync`、`cvcuda.Tensor.cuda`、`cvcuda.as_tensor`……)覆盖了算子之外容易成为瓶颈的「胶水操作」,让整条管线的 CPU 时间被完整切分,不留无名空白。

#### 4.3.2 核心流程

一次带同步的 flip 调用在双轨时间线上的形态(示意):

```
CPU 线程轨:  [cvcuda.flip ██████[cvcudaFlipSubmit ███]████]──[cvcuda.Stream.sync ████████████]──→
                        ↑ 内外层差 = 绑定开销        ↑ CPU 空转等 GPU
GPU 流轨:    ·········(空闲)········[FlipKernel ███████]········→
```

伪代码式的分析流程:

```
1. nsys profile 捕获 → 得到 .nsys-rep 报告
2. 打开时间线,找到 NVTX 行(按线程分组)
3. 对每个感兴趣的区间读两件事:
   a. CPU 侧长度(提交/等待成本)
   b. 它引发的 kernel 在 GPU 行的长度(计算成本)
4. 扫 GPU 行找空隙 → 回查空隙期间 CPU 在哪个 NVTX 区间里 → 空隙的"责任人"
5. 常见结论:空隙对应 nvimgcodec 编码/解码、Python 循环、cudaStreamSynchronize 等
```

nsys 常用命令(报告名随 nsys 版本可能略有差异,以 `nsys stats --help` 列出的为准):

```bash
nsys profile -o hw --trace=cuda,nvtx python3 samples/applications/hello_world.py
nsys ui hw.nsys-rep                    # 图形界面看时间线
nsys stats -r nvtx_kern_sum hw.nsys-rep   # 按 NVTX 区间归组统计 kernel 时间
```

其中 `nvtx_kern_sum` 类报告的价值在于:它把每个 NVTX 区间内启动的 kernel 时间归到该区间名下——正好补上 2.2 节说的「NVTX 记 CPU 时间、不记 GPU 时间」的缺口,把「cvcuda.gaussian 名下 GPU 实际跑了多少毫秒」直接列成表。

#### 4.3.3 源码精读

嵌套顺序不是文档口头约定,而是被测试钉死的行为。[tests/cvcuda/python/test_nvtx_markers.py:177-192](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_nvtx_markers.py#L177-L192):

```python
@requires_probe
def test_operator_python_range_wraps_submit():
    """An injected probe observes the Python range followed by the C-API submit range."""
    _reset_probe()
    _flip()
    captured = _captured_ranges()
    assert "cvcuda.flip" in captured, ...
    assert "cvcudaFlipSubmit" in captured, ...
    assert captured.index("cvcuda.flip") < captured.index("cvcudaFlipSubmit"), \
        "Python range must wrap the submit range. Captured: {captured}"
```

探针只记录 push 的名字(见 4.4 节),所以「`cvcuda.flip` 的 push 先于 `cvcudaFlipSubmit` 的 push」即证明外层先开、内层后开——嵌套关系成立。(push/pop 的严格配对由 RAII 保证,这里无需再验。)

Stream 与容器埋点的契约同样有断言守护:[tests/cvcuda/python/test_nvtx_markers.py:203-220](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_nvtx_markers.py#L203-L220) 验证 `cvcuda.Stream.__enter__`/`__exit__`/`sync`/`wait_stream` 四个区间;[L223-255](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_nvtx_markers.py#L223-L255) 验证 `cvcuda.Stream.create`、`cvcuda.Tensor.cuda`、`cvcuda.as_tensor`、`cvcuda.reshape`、`cvcuda.Image.cuda`/`cpu` 等九个区间。

顺带辨析:CV-CUDA 仓库里还有**另一套** NVTX 体系——样例/基准用的 Python 端 `CvCudaPerf` 类([bench/python/perf_utils.py:41](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/perf_utils.py#L41)、[L105-123](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/perf_utils.py#L105-L123)),它经 `nvtx.push_range/pop_range` 给**应用自己的管线阶段**(如"预处理整体")打标。它与本讲的库内埋点是互补关系:库埋点细化到单算子,应用埋点框住业务阶段,两层叠加才是完整的时间线叙事。u7-l3 已指出它属于样例管线体系、与基准计时流是两回事。

#### 4.3.4 代码实践(源码阅读 + 运行,待本地验证)

1. **实践目标**:在不装 Nsight Systems 的前提下,先把「一次调用会 push 哪些区间、顺序如何」完整观察一遍,为看懂时间线做准备。
2. **操作步骤**:
   - 按仓库方式构建测试产物(含探针,见 4.4 节的 CMake 目标 `cvcuda_nvtx_probe`);
   - 以探针路径启动一个独立 Python 进程:
     ```bash
     NVTX_INJECTION64_PATH=/path/to/libcvcuda_nvtx_probe.so \
       python3 -c "
     import ctypes, cvcuda, numpy as np, cupy as cp
     lib = ctypes.CDLL('/path/to/libcvcuda_nvtx_probe.so')
     lib.CvcudaNvtxProbe_Count.restype = ctypes.c_uint
     lib.CvcudaNvtxProbe_Name.restype = ctypes.c_char_p
     buf = cp.zeros((1,64,64,3), dtype=cp.uint8)
     t = cvcuda.as_tensor(buf, 'NHWC')
     lib.CvcudaNvtxProbe_Reset()
     cvcuda.flip(t, 1); cvcuda.flip_into(t, t, 0)
     for i in range(lib.CvcudaNvtxProbe_Count()):
         print(lib.CvcudaNvtxProbe_Name(i).decode())
     "
     ```
   - 对比打印顺序与 4.3.1 的洋葱结构。
3. **需要观察的现象**:输出形如 `cvcuda.flip → cvcudaFlipSubmit → cvcuda.flip_into → cvcudaFlipSubmit`,即每次调用外层 Python 名在前、内层 C API 名紧随。
4. **预期结果**:顺序与 `test_operator_python_range_wraps_submit` 的断言一致;若探针未加载(环境变量路径不对),不会报错但列表为空——NVTX 注入是静默可选的。
5. 本环境无 GPU 与构建产物,运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:时间线上看到 `cvcuda.flip` 区间长达 2ms,而对应 `FlipKernel` 在 GPU 行只有 20µs,这说明什么?该往哪个方向优化?

**答案**:提交成本远大于计算成本,CPU 侧是瓶颈。可能是对象缓存未命中导致真实发生了 cudaMalloc、Python 循环逐张调用太碎、或 pybind11 转换开销。方向:预分配 + `flip_into`(u3-l3)、批处理合并调用、核对 u4-l2 的缓存命中条件。反之若 kernel 时间远大于区间,则是 kernel 本身慢,归 u7-l3/u8-l4 的算子优化流程管。

**练习 2**:为什么 `test_operator_python_range_wraps_submit` 只断言 push 顺序,而不检查 pop?

**答案**:探针的 `ProbeRangePop` 是空实现、什么都不记(见 4.4 节源码),记录流里只有 push 的名字序列。push 顺序已足以证明「外层先开、内层后开」;而「区间正确关闭」由 RAII 析构在类型层面保证,不是需要在运行时验证的行为。

### 4.4 让埋点可被测试:NvtxProbe 注入库与守护测试

#### 4.4.1 概念说明

2.1 节说过 NVTX 只写不读:分析器能读,你自己的测试进程却拿不到区间数据。可是 CV-CUDA 有 480 多处埋点,靠什么保证没人新增算子时忘了埋、或改动时埋错了名字?

答案是两条互补防线:

1. **静态扫描**(不走 GPU、不需要探针):测试用正则直接检查源码文本——每个 Python 绑定必须有 `NvtxTrace`、每个 C API Submit 定义第一行必须是 `CVCUDA_NVTX_RANGE`。
2. **运行时探针**:一个约 150 行的注入库,借 NVTX 官方的注入机制替换掉 `nvtxRangePushA` 的实现,把名字记进向量,测试再用 `ctypes` 读出来断言真实运行时行为。

#### 4.4.2 核心流程

NVTX 注入机制的流程:

```
进程启动,NVTX 库首次被调用
  → 读环境变量 NVTX_INJECTION64_PATH,得一个 .so 路径
  → dlopen 该 .so,调用其导出函数 InitializeInjectionNvtx2(getExportTable)
  → 注入库经 export table 拿到 core 模块的"函数指针槽位表"
  → 把 RangePushA / RangePop 两个槽位改写为自己的 ProbeRangePushA / ProbeRangePop
  → 返回 1 表示注入成功
此后全进程的 nvtxRangePushA("...") 实际执行的是记录函数
测试进程(同一个进程,因为 .so 是被 dlopen 进来的)用 ctypes 调导出的访问器读取
```

关键认知:注入只发生一次、进程启动时;函数表里每个元素是**槽位的地址**,所以替换要「解引用后写入」(`*table[...] = &Probe;`)。

#### 4.4.3 源码精读

记录函数与缓冲上限([tests/cvcuda/nvtx_probe/NvtxProbe.cpp:55-69](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/nvtx_probe/NvtxProbe.cpp#L55-L69)):

```cpp
int NVTX_API ProbeRangePushA(const char *message)
{
    std::scoped_lock lock(state().mutex);
    if (state().pushedNames.size() < kMaxRecordedNames)   // 4096 上限
    {
        state().pushedNames.emplace_back(message ? message : "");
    }
    return static_cast<int>(state().pushedNames.size());
}

int NVTX_API ProbeRangePop(void)
{
    return 0;   // pop 不记录:push 序列已足够断言
}
```

第 37 行的 `kMaxRecordedNames = 4096` 及其注释([L32-37](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/nvtx_probe/NvtxProbe.cpp#L32-L37))解释了上限的由来:注入路径进程启动时只读一次,探针会伴随宿主进程终身;一个完整的 pytest 会话驱动全部算子、外加 cupy 自己的 NVTX 区间,若无上限,记录向量会无界增长,在小内存 runner 上可能耗尽宿主内存。到达上限后只是停止记录,不报错。

注入入口([tests/cvcuda/nvtx_probe/NvtxProbe.cpp:82-121](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/nvtx_probe/NvtxProbe.cpp#L82-L121))做了层层防御:export table 为空、结构尺寸不符、`GetModuleFunctionTable` 失败、表尺寸不含 `RangePop` 槽位——任一情况都返回 0(注入失败,NVTX 回退到原实现,程序照常运行):

```cpp
extern "C" int InitializeInjectionNvtx2(NvtxGetExportTableFunc_t getExportTable)
{
    // ...取 callbacks、取函数表、校验尺寸(略)...
    if (table[NVTX_CBID_CORE_RangePushA] != nullptr)
    {
        *table[NVTX_CBID_CORE_RangePushA] =
            reinterpret_cast<NvtxFunctionPointer>(&ProbeRangePushA);
    }
    // ...同理替换 RangePop...
    return 1;
}
```

访问器([tests/cvcuda/nvtx_probe/NvtxProbe.cpp:125-148](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/nvtx_probe/NvtxProbe.cpp#L125-L148))导出三个 C 函数:`CvcudaNvtxProbe_Reset`(清空)、`CvcudaNvtxProbe_Count`(计数)、`CvcudaNvtxProbe_Name(i)`(取第 i 个名字)。注释说明指针只在下一次 Reset 前有效——测试在算子调用完成后读取,期间无并发 push,存储是稳定的。

构建方式很能说明「注入库」的性质([tests/cvcuda/nvtx_probe/CMakeLists.txt:16-27](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/nvtx_probe/CMakeLists.txt#L16-L27)):建成 `MODULE` 库(注释明言:只被 NVTX 经由 `NVTX_INJECTION64_PATH` dlopen,永不参与链接),并随测试安装到 python 测试目录,让打包安装(DEB/TAR)后的测试也能注入。

测试侧的加载与使用([tests/cvcuda/python/test_nvtx_markers.py:45-54](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_nvtx_markers.py#L45-L54)):

```python
def _load_probe():
    path = os.environ.get("NVTX_INJECTION64_PATH")
    if not path or not os.path.exists(path):
        return None
    lib = ctypes.CDLL(path)          # dlopen 同一个 .so → 拿到的是同一份记录状态
    ...
```

注意精妙之处:测试经 `ctypes.CDLL` 再加载一次同一路径的 .so,操作系统对同一文件的动态库只映射一份,所以测试进程与注入点共享同一个 `ProbeState` 静态变量——读到的正是 NVTX 替换函数记下的内容。探针不可用时,`requires_probe` 标记([L58-61](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_nvtx_markers.py#L58-L61))让运行时测试整体跳过而非失败。

静态扫描防线的核心思路([tests/cvcuda/python/test_nvtx_markers.py:123-174](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_nvtx_markers.py#L123-L174)):先用 `_public_operator_names()` 从 `dir(cvcuda)` 里筛出「名字叫 `x` 且存在 `x_into`」的公开算子集合,再对 `python/mod_cvcuda/operators/Op*.cpp` 全量匹配 `m.def("name"` 与紧随其后的 `NvtxTrace("cvcuda.name"`;对 C API 则匹配头文件里的 `CVCUDA_PUBLIC NVCVStatus cvcudaXxxSubmit(` 声明与源文件里 `CVCUDA_DEFINE_API(... cvcudaXxxSubmit ...)` 定义,并要求定义函数体开头是 `CVCUDA_NVTX_RANGE("cvcudaXxxSubmit")`。任何新增算子漏埋点、或区间名与函数名不一致,CI 直接点名文件与行号。

最后是运行编排的取舍([tests/cvcuda/python/cvcuda_test_python.in:75-84](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_test_python.in#L75-L84)、[L110-120](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/cvcuda_test_python.in#L110-L120)):主测试批次先 `unset NVTX_INJECTION64_PATH` 再跑**全部**测试(排除 marker 测试),随后单独起一个 pytest 进程、只带注入跑 `test_nvtx_markers.py`。注释写明原因:注入若覆盖整个会话,每个算子调用和 cupy 的区间都会流经探针,只有 marker 测试会 Reset,记录缓冲会无界增长、威胁宿主内存。这正是「按需注入、进程隔离」的一次示范。

#### 4.4.4 代码实践(源码阅读 + 运行)

1. **实践目标**:亲眼看两条防线工作——静态扫描能揪出"没埋点的绑定",运行时探针能列出真实区间。
2. **操作步骤**:
   - 静态:运行 `pytest tests/cvcuda/python/test_nvtx_markers.py -k "instrumented" -v`(无需 GPU、无需探针;源码目录存在时执行);
   - 思想实验:在脑中给 `python/mod_cvcuda/operators/OpFlip.cpp:96` 删掉 `NvtxTrace` 包装(不要真改仓库),预测两个静态测试各自报什么、报在哪一行;
   - 运行时:按 4.3.4 的方式注入探针,跑 `pytest tests/cvcuda/python/test_nvtx_markers.py`(需要构建产物与 GPU,**待本地验证**)。
3. **需要观察的现象**:静态测试通过时输出 collected/passed;思想实验中应能推断出 `untraced` 列表会精确给出 `python/mod_cvcuda/operators/OpFlip.cpp:96` 这样的 `文件:行号`(由测试的 `_location` 辅助函数生成,见 [test_nvtx_markers.py:118-120](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/python/test_nvtx_markers.py#L118-L120))。
4. **预期结果**:两条防线分别覆盖「漏埋」「埋错名」与「运行时顺序/存在性」三类回归;探针缺失时运行时用例显示 skip 而非 error。

#### 4.4.5 小练习与答案

**练习 1**:为什么探针建成 `MODULE` 库而不是普通 `SHARED` 库,并且不链接任何 CV-CUDA 目标?

**答案**:`MODULE` 产物是「只能被 dlopen、不能被链接」的动态库,语义上与它的真实用法完全一致——它只经 `NVTX_INJECTION64_PATH` 被 NVTX dlopen。它不依赖任何 CV-CUDA 符号(只用 NVTX 头与标准库),因此可以注入**任意**宿主进程,包括别的应用;唯一链接的 `cvcuda_nvtx_config` 只是头文件路径的 INTERFACE 库。

**练习 2**:`ProbeRangePushA` 为什么要加互斥锁、还要容忍 `message == nullptr`?

**答案**:多线程程序里多个 CPU 线程可以并发 push(如 u4-l3 的多线程测试),不加锁向量会损坏;而 NVTX 规范允许传空指针,探针作为替换实现必须至少不崩——用空字符串兜底。`message ? message : ""` 正是这一防御。

## 5. 综合实践

**任务**:用 Nsight Systems 剖析 `hello_world.py`,量化六步流水线各阶段占比,并指出可并行化的串行等待。

**前置**:带 GPU 的机器、已安装 `cvcuda`(u1-l2)、`nsys`(Nsight Systems CLI)、`samples/requirements.txt` 中的依赖。本环境无 GPU,以下结果**待本地验证**。

1. **实践目标**:把本讲全部知识串成一次真实剖析——找区间、对齐双轨、算占比、找空隙。

2. **操作步骤**:

   ```bash
   cd samples/applications
   nsys profile -o hw_report --trace=cuda,nvtx \
       python3 hello_world.py -i ../assets/images/tabby_tiger_cat.jpg -o /tmp/out.jpg
   nsys ui hw_report.nsys-rep              # 打开图形时间线
   # 无图形环境时用统计报告(报告名以 nsys stats --help 实列为准):
   nsys stats -r nvtx_kern_sum hw_report.nsys-rep
   nsys stats -r cuda_gpu_kern_sum hw_report.nsys-rep
   ```

3. **需要观察的现象**:

   - NVTX 行上依次出现:`cvcuda.as_tensor`(u1-l2 讲过解码产物包装)、`cvcuda.resize` ⊃ `cvcudaResizeSubmit`、`cvcuda.stack` ⊃ `cvcudaStackSubmit`、`cvcuda.gaussian` ⊃ `cvcudaGaussianSubmit`,最后 `cvcuda.Tensor.cuda`(交给 nvimgcodec 编码);
   - 对照 [samples/applications/hello_world.py:182-235](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L182-L235) 的六个阶段与脚本自带的 `timer` 打印([L77-90](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L77-L90))——后者给出墙钟分组,前者给出细粒度分解;
   - nvimgcodec 的解码/编码不是 CV-CUDA 算子,没有库内区间;识别它们要靠 CUDA 运行时 API 行(如 memcpy、NPP/自研 kernel)与 `timer` 输出的阶段分组。

4. **占比统计方法**:在 `nsys ui` 中框选整个管线,逐个右键 NVTX 区间读时长;或直接用 `nvtx_kern_sum` 表格把 GPU kernel 时间按区间名归组。把六个阶段(解码、resize、stack、gaussian、split、编码)各自的总时长除以全程墙钟,得到占比表。

5. **预期结果与串行等待分析**:

   - `hello_world.py` 是**单流串行**管线:六步顺序依赖,且每步 Python 循环逐张处理(如 [L197-204](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/samples/applications/hello_world.py#L197-L204) 的逐张 resize)。时间线上大概率能看到:GPU 行在 Python 忙于 CPU 侧工作(解码、参数解析、编码)时长时间空闲;
   - 典型可并行化点(按值得排查的顺序):
     1. **解码与上一张的处理重叠**:多张输入时,用第二条流在 GPU 处理第 i 张的同时解码第 i+1 张(u4-l1 的 `wait_stream`/多流知识);
     2. **逐张 resize 改批处理**:同尺寸输入可先 `cvcuda.stack` 再一次 resize,减少 Python 循环与 kernel 启动次数(与 u3-l4 的融合算子思路呼应);
     3. **编码写盘等待**:编码通常占尾段时间,可与下一帧的前段重叠。
   - 把你找到的最大 GPU 空闲段截图,标注它落在哪个 CPU 区间里,写一段结论:空闲的责任人是解码、Python 循环还是同步等待。

## 6. 本讲小结

- CV-CUDA 的 NVTX 埋点是全库纪律:C++ 侧每个 `*Submit` 函数体第一行一个 `CVCUDA_NVTX_RANGE`(228 处/122 文件),Python 侧每个 `m.def` 都包 `NvtxTrace`(约 254 处/66 文件),区间名即函数名,时间线上可直接反查源码。
- 两侧实现都是 RAII(构造 push、析构 pop、禁止拷贝),异常安全;Python 侧因不能 include 私有头而复制了一份助手类,并用「签名保持」的模板包装器做到零侵入绑定。
- 埋点覆盖不止算子:`cvcuda.Stream.__enter__/__exit__/sync/wait_stream`、`cvcuda.Tensor.cuda`、`cvcuda.as_tensor` 等胶水操作也有区间,其中 `Stream.sync` 直接量化 CPU 等待 GPU 的时间。
- NVTX 区间记录的是 **CPU 侧时间**;GPU 执行看 CUDA 行,两层嵌套(`cvcuda.flip` ⊃ `cvcudaFlipSubmit`)之差即绑定层开销,`nvtx_kern_sum` 类报告可把 kernel 时间按区间名归组。
- 埋点的正确性由两条防线守护:静态源码扫描(漏埋/错名直接点名文件行号)+ 注入式探针(`NVTX_INJECTION64_PATH` 替换 NVTX 函数表,只记 push 名字,4096 上限防膨胀,单独进程注入避免污染整套测试)。
- 性能分析方法论:基准(u7-l3)发现「有没有回归」,nsys 时间线回答「谁在等谁」;GPU 空闲段 + 其上方 CPU 区间名,就是优化线索。

## 7. 下一步学习建议

- **动手巩固**:对你在 u3/u9 里写过的管线做一次同样的 nsys 剖析,对比单流与双流版本时间线上的 GPU 空闲段差异;若发现某个算子 kernel 时间异常,进入下一讲的优化流程。
- **下一讲 u8-l1(新增算子:mkop 脚手架)**:你会看到新算子骨架自带的绑定模板里就包含 `NvtxTrace` 包装——本讲的静态扫描测试正是强制这一点;学完后你能在自己新增的算子里正确延续埋点纪律。
- **继续阅读的源码**:对照读 [python/mod_cvcuda/operators/OpResize.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/python/mod_cvcuda/operators/OpResize.cpp)(四个重载四种区间名)与 [src/cvcuda/OpResize.cpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpResize.cpp);进阶可读 [bench/python/perf_utils.py](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/bench/python/perf_utils.py) 看应用层 NVTX(`CvCudaPerf`)如何与库层区间叠加成完整叙事。
- **外部工具**:Nsight Systems 文档中关于 NVTX correlation、GPU 空闲分析与 `--trace` 选项的章节;以及 NVTX 注入(`NVTX_INJECTION64_PATH`)机制的官方说明——本讲 4.4 的探针是它的一次精炼示范。

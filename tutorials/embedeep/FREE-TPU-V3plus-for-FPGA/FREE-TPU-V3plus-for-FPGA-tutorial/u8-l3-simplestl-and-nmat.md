# simplestl 与 nmat：自实现容器与内存对齐

## 1. 本讲目标

在 u5-l2 里，我们把 TPU 的原生 epmat 输出反量化成浮点 `ncnn::Mat`，随后这个 Mat 又被塞进一个 `vector`、交给 `partial_sort` 做 topk。当时我们把这些容器当成「理所当然存在」的黑盒用掉了。本讲要打开这两个黑盒：

- `simplestl`：裸机工程自带的「精简版 STL」，提供 `vector`、`string`、`pair`、`list`、`partial_sort`、`greater/less`，以及全局 `operator new/delete`。
- `nmat.h`：从腾讯 ncnn 推理框架裁出来的最小 `ncnn::Mat` 实现，含 `fastMalloc`、`alignSize`、`alignPtr` 等对齐工具。

学完后你应当能够：

1. 说清裸机/嵌入式工程为何要「自己造一套 STL」，以及它替代标准库的代价与边界。
2. 读懂 `ncnn::Mat` 的 `dims/w/h/c/cstep` 字段与 `channel(c)` 视图模型，理解它如何为「按通道分块、每块对齐」的访问服务。
3. 手写/推演 `fastMalloc` 的「超额申请 + 对齐 + 藏指针」技巧。
4. 用幂二次方对齐数学 \((x+n-1)\ \&\ \sim(n-1)\) 解释 `alignSize`/`alignPtr`，并说清 `MALLOC_ALIGN=16` 为何对 TPU 的 16 通道 epmat 数据友好。

本讲属专家层（advanced），它不引入新的硬件协议，而是把前面所有讲义里用到的「软件基础设施」补齐，让你具备二次开发与跨平台移植这两块代码的能力。

## 2. 前置知识

- **C++ 的 `new`/`delete` 与 `malloc`/`free` 的关系**：`new` 在底层会调用全局 `operator new(size_t)` 来获取内存，再调用构造函数；标准库 `libstdc++`/`libc++` 提供了这两个运算符的默认实现（内部就是 `malloc`）。裸机环境往往链接不到完整 C++ 标准库，于是必须自己提供 `operator new/delete`，否则任何用 `new` 的代码都链接不过。
- **字节对齐（alignment）**：一个地址是「n 字节对齐」指它是 n 的整数倍。许多硬件（DMA、NEON/SIMD、某些 load 指令）要求操作数对齐，未对齐会触发异常或性能暴跌。
- **NCNN**：腾讯开源的神经网络推理框架，其 `ncnn::Mat` 是一个支持引用计数、按通道分块、内存对齐的张量类。本工程没有引入整个 ncnn，只把 `Mat` 这一棵树挪了过来。
- **epmat 与 16 通道分组**（来自 u4-l4 / u5-l2）：TPU 以「16 通道 × 2 字节 = 32 字节」为最小访存单元，把张量按 16 通道一组打包。
- **位运算与补码**：对正整数 n 满足 \(-n = \sim(n-1)\)（二进制补码），这是后续对齐公式的基础。

> 阅读建议：本讲四个最小模块按「上层容器 → 张量类型 → 分配器 → 对齐数学」自顶向下展开，但理解时不妨自底向上——先看懂第 4.4 节的对齐数学，第 4.3 的 `fastMalloc` 与第 4.2 的 `cstep` 就一目了然。

## 3. 本讲源码地图

| 文件 | 角色 | 关键内容 |
| --- | --- | --- |
| `sdk/standalone/src/simplestl.h` | 头文件式精简 STL | 全局 `operator new/delete` 声明；`std` 命名空间下的 `max/min/swap`、`pair`、`list`、`vector`、`string`、`greater/less`、`partial_sort` |
| `sdk/standalone/src/simplestl.cpp` | `operator new/delete` 实现 | 把 C++ 的 `new/delete` 接到裸机 `malloc/free` 上 |
| `sdk/standalone/src/eeptpu/nmat.h` | 最小 `ncnn::Mat` | `alignPtr`、`alignSize`、`fastMalloc/fastFree`、`Mat` 类（dims/w/h/c/cstep、引用计数、`channel()` 视图） |
| `sdk/standalone/src/eeptpu/eeptpu_sa.cpp`（消费侧，非本讲核心） | 用法佐证 | `epmat2nmat` 用 `Mat::channel(c)` 写每通道；`read_forward_result` 用 `vector<ncnn::Mat>` 收集输出 |
| `sdk/standalone/src/post_process/classify.cpp`（消费侧） | 用法佐证 | `get_topk` 用 `vector<pair<float,int>>` + `partial_sort` + `greater` |

## 4. 核心概念与源码讲解

### 4.1 自实现 vector/string：为什么裸机要自带 STL

#### 4.1.1 概念说明

在 PC/Linux 上写 C++，`#include <vector>` 即可，因为系统里有完整的 `libstdc++`。但在 ZynqMP 裸机（standalone）工程里：

- 工具链是 Xilinx Vitis 的 ARM 编译器，BSP（板级支持包）只提供 C 运行时与 `malloc/free`，**不保证链接完整 C++ 标准库**。
- 标准 `libstdc++` 体积大，还会牵扯异常处理（`-fexceptions`）、线程、locale、文件流等裸机用不上、也跑不起来的东西。
- 推理代码（来自 ncnn 的移植）大量用了 `std::vector`、`std::pair`、`std::string`、`std::partial_sort`，直接禁掉这些就要重写一大片业务逻辑。

折中方案就是 `simplestl`：在 `namespace std` 里手写一套**够用就好**的容器与算法，把名字占住，让原本依赖标准库的代码几乎不用改就能编译。这是嵌入式 C++ 工程里非常典型的「自带精简标准库」做法。

#### 4.1.2 核心流程

```
C++ 源码里写 std::vector / new / std::partial_sort
        │
        ├─ new/delete ──► 全局 operator new/delete（simplestl.cpp）
        │                       └─► 调用裸机 malloc / free
        │
        └─ std::vector / pair / string / partial_sort ──► simplestl.h 里的模板实现
                                                              （不依赖 libstdc++）
```

第一步是「接通内存」：必须有人提供全局 `operator new/delete`，否则链接器报 `undefined reference to operator new`。这一步在 `simplestl.cpp` 里完成。第二步是「补齐容器与算法」，在 `simplestl.h` 里完成。

#### 4.1.3 源码精读

**① 把 `new`/`delete` 接到 `malloc`/`free`**

simplestl.cpp 里所有全局 `operator new` 都是 `malloc` 的一层薄包装：

```cpp
void* operator new(size_t sz) noexcept { void* ptr = malloc(sz); return ptr; }
void* operator new(size_t sz, void* ptr) noexcept { return ptr; }   // placement new
void operator delete(void *ptr) noexcept { free(ptr); }
```

参见 [sdk/standalone/src/simplestl.cpp:L22-L62](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/simplestl.cpp#L22-L62)，这里实现了 4 个 `new`（普通/数组、各带一个 placement 版）与 4 个 `delete`。注意第 28–31 行的 placement `new(size_t, void*)`——它**直接返回传入指针**、不分配，这是「在已分配内存上构造对象」的语义，`vector::resize` 就靠它做原地构造（见下文）。

**② `operator new/delete` 的声明**

在头文件里先声明，供所有翻译单元共享：[sdk/standalone/src/simplestl.h:L28-L36](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/simplestl.h#L28-L36)。

**③ `std::vector` 的存储与扩容**

`vector` 用裸指针 `data_`、`size_`、`capacity_` 三件套管理一段连续内存（[simplestl.h:L489-L492](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/simplestl.h#L489-L492)），核心是 `try_alloc`：

```cpp
void try_alloc(size_t new_size)
{
    if (new_size * 3 / 2 > capacity_ / 2)        // 判定是否需要扩容
    {
        capacity_ = new_size * 2;                // 新容量 = 需求 × 2
        T* new_data = (T*)new char[capacity_ * sizeof(T)];
        memset(new_data, 0, capacity_ * sizeof(T));
        if (data_) { memmove(new_data, data_, sizeof(T) * size_); delete[](char*) data_; }
        data_ = new_data;
    }
}
```

参见 [simplestl.h:L493-L507](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/simplestl.h#L493-L507)。这里有两点值得读懂：

- 内存以 `new char[]` 申请、再以 `delete[](char*)` 释放——刻意走「字节数组」而不是 `new T[]`，是为了**绕过元素构造/析构**，把对象生命周期交给 `resize`/`clear` 用 placement new / 显式析构手动管理（见 `resize` 第 399、406 行的 `new (&data_[i]) T(value)` 与 `data_[i].~T()`）。这正是上面 placement new 的用武之地。
- 扩容判定 `new_size * 3 / 2 > capacity_ / 2` 等价于 `new_size * 3 > capacity_`，即「只要需求超过容量的三分之一就重分配」。配合 `capacity_ = new_size * 2`，你会发现在**反复 `push_back`** 时它几乎每次都重分配——这是一个偏保守、并不高效的生长策略。但本工程实际用法是 `resize(N)` 一次性定长、再按下标写（例如 [classify.cpp:L29](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/post_process/classify.cpp#L29) 的 `vec.resize(size)`），扩容路径几乎不被触发，所以这点低效在实践中无伤大雅。读自实现容器时，要养成「看它怎么用，而不只看它怎么写」的习惯。

**④ `partial_sort`：标注了 TODO 的冒泡实现**

topk 用到的 `std::partial_sort` 在 simplestl 里是这样实现的：

```cpp
template<typename RandomAccessIter, typename Compare>
void partial_sort(RandomAccessIter first, RandomAccessIter middle, RandomAccessIter last, Compare comp)
{
    // [TODO] heap sort should be used here, but we simply use bubble sort now
    for (auto i = first; i < middle; ++i)
        for (auto j = last - 1; j > first; --j)
            if (comp(*j, *(j - 1))) swap(*j, *(j - 1));
}
```

参见 [simplestl.h:L337-L352](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/simplestl.h#L337-L352)。作者在注释里老实承认「本该用堆排序，现在先用冒泡」。复杂度从标准库的 \(O(n\log k)\) 退化成 \(O(n\cdot k)\)，但对分类网络 \(n=1000\)、\(k=5\) 的 topk 来说完全够用。这也是自实现容器的典型取舍：**够用、可控、零依赖**，胜过「功能完整但拉入巨量依赖」。

**⑤ `string` = `vector<char>` + C 字符串接口**

`string` 直接公有继承 `vector<char>`，再补 `c_str()`、`operator==`、`operator+=` 等，见 [simplestl.h:L510-L542](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/simplestl.h#L510-L542)。它复用了 vector 的全部存储逻辑，是一个很经济的实现。注意它的 `c_str()` 直接返回 `data_`，**不保证以 `\0` 结尾**（因为 vector 不主动补终止符），用 `strcmp` 时要小心。

#### 4.1.4 代码实践

**实践：验证 simplestl 真能替代 `std::vector`**

1. 实践目标：理解 simplestl 提供的 `vector` 与标准库行为一致到什么程度，并亲眼看到「没有它，C++ 代码就链接不过」。
2. 操作步骤（源码阅读型，待本地验证）：
   - 在 [classify.cpp:L23-L47](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/post_process/classify.cpp#L23-L47) 的 `get_topk` 里，找出它用到的全部 std:: 符号：`vector`、`pair`、`make_pair`、`partial_sort`、`greater`。逐一在 `simplestl.h` 中定位它们的定义行。
   - 假想「删掉 `#include "../simplestl.h"`」：列出链接期会缺失的符号（提示：首先是 `operator new`，其次是 `std::vector<...>::resize` 等模板实例）。
3. 需要观察的现象：classify.cpp 顶部**只** include 了 `simplestl.h` 与 `nmat.h`，没有任何 `<vector>`/`<algorithm>`——说明它完全靠 simplestl 提供这些符号。
4. 预期结果：你能用一张表把「classify.cpp 用到的每个 std:: 符号 ↔ simplestl.h 中的定义行号」一一对应，证明 simplestl 已完整覆盖该文件的容器/算法需求。

#### 4.1.5 小练习与答案

**练习 1**：`vector::try_alloc` 用 `new char[]` 而不是 `new T[]` 申请内存，为什么？

**参考答案**：`new T[]` 会对每个元素调用默认构造、`delete[]` 会逐个析构，而 simplestl 希望把「分配」与「构造」解耦——分配只拿一块原始内存，构造/析构交给 `resize` 用 placement new 与显式 `~T()` 精确控制。用 `char` 数组就绕开了元素级的自动构造析构。

**练习 2**：`std::partial_sort(v.begin(), v.begin()+5, v.end(), greater{})` 在 simplestl 实现下，最坏比较次数是多少？若用标准库的堆实现呢？

**参考答案**：simplestl 是冒泡：外层 5 次、内层约 \(n\) 次，约 \(5n\) 次比较，即 \(O(n\cdot k)\)。标准库用堆，为 \(O(n\log k)\)。当 \(n=1000,k=5\) 时分别是约 5000 次与约 1000×2.3≈ 几千次，差距不大；但 n 很大时差距会拉开。

---

### 4.2 ncnn::Mat 通道模型：dims / w / h / c / cstep 与 channel()

#### 4.2.1 概念说明

`ncnn::Mat` 是一个轻量张量类，本工程只保留了它的「骨架」：一段对齐内存 + 几个描述字段 + 引用计数。它解决两个问题：

- 给后处理算子（topk、yolo 解码、NMS）提供一个**统一的浮点张量抽象**，而不是让大家各自 `float*` 乱传。
- 用「按通道分块 + 每块对齐」的内存布局，让「按通道遍历」这种神经网络最常见的访问模式既高效又好写。

理解 Mat 的关键是 5 个几何字段：`dims`（维度数）、`w/h/c`（宽、高、通道）、`cstep`（每个通道平面的**元素**步长）。其中 `cstep` 是最容易踩坑、也最重要的字段。

#### 4.2.2 核心流程

Mat 按维度数分三种形态，由构造函数/create 重载区分：

```
dims=1  向量        : 只有 w            ，cstep = w            （平面大小 = w 个元素）
dims=2  图像(单通道): w × h            ，cstep = w*h
dims=3  张量(多通道): w × h × c        ，cstep = alignSize(w*h*elemsize,16)/elemsize
```

对 dims=3 的多通道张量，内存被想象成 c 个「通道平面」首尾相接，每个平面装 w×h 个元素，但平面之间会**补齐到 16 字节的整数倍**（由 `cstep` 体现）。访问第 c 个通道用 `channel(c)`，它返回一个**指向同一块内存**的轻量视图（不拷贝）：

```
data ──► [ plane0: w*h 元素 + padding ][ plane1 ][ plane2 ] ...
            ▲                            ▲
          channel(0)                   channel(1) = data + cstep*1*elemsize
```

引用计数 `refcount` 让 Mat 可以廉价拷贝（拷贝只共享指针、`refcount++`），在最后一个引用析构时才真正释放内存。

#### 4.2.3 源码精读

**① Mat 的全部字段**

```cpp
void* data;        // 指向张量数据（由 fastMalloc 分配，16 字节对齐）
int*   refcount;   // 引用计数指针；指向外部内存时为 NULL
size_t elemsize;   // 单元素字节数：4=float32, 2=float16, 1=int8
Allocator* allocator;
int dims;          // 1/2/3
int w, h, c;
size_t cstep;      // 每个通道平面的「元素」步长（不是字节！）
```

参见 [nmat.h:L173-L196](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L173-L196)。务必记住 `cstep` 的单位是**元素个数**，不是字节——字节偏移要再乘 `elemsize`。

**② dims=3 时 cstep 的对齐计算**

三维构造函数与 `create(w,h,c)` 里都这样算 `cstep`：

```cpp
cstep = alignSize(w * h * elemsize, 16) / elemsize;
```

参见三维 external-data 构造 [nmat.h:L256-L264](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L256-L264) 与 `create(w,h,c)` [nmat.h:L375-L402](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L375-L402)。先把「一个平面的字节数 `w*h*elemsize`」向上对齐到 16 字节，再除回元素个数。效果是：**每个通道平面都占 16 字节整数倍的内存**。举例（float，elemsize=4，yolov4-tiny 的 13×13 输出）：

\[ 13\times13\times4 = 676 \text{ 字节} \xrightarrow{\text{alignSize}(\cdot,16)} 688 \text{ 字节} \xrightarrow{/4} cstep = 172 \text{ 元素} \]

即每通道实际占 172 个 float（169 个有效 + 3 个 padding），保证下一通道起点仍是 16 字节对齐。

**③ channel(c)：零拷贝的通道视图**

```cpp
inline Mat Mat::channel(int c)
{
    return Mat(w, h, (unsigned char*)data + cstep * c * elemsize, elemsize, allocator);
}
```

参见 [nmat.h:L444-L452](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L444-L452)。它调用的是「external data」构造函数，`refcount=0`，所以返回的临时 Mat **不持有内存、不影响引用计数**，析构时不会误释放。这正是 u5-l2 里 `epmat2nmat` 能写 `float* pdst = (float*)dstmat.channel(c).data;` 逐通道写入的原理——每次拿到第 c 个平面的起点指针，往里灌反量化后的浮点。

**④ 引用计数：拷贝即共享**

拷贝构造里共享 data 并把 `refcount` 加一，析构走 `release()` 减一、到 0 才 `fastFree`：

```cpp
inline Mat::Mat(const Mat& m) : data(m.data), refcount(m.refcount), ... {
    if (refcount) NCNN_XADD(refcount, 1);
    ... 
}
```

参见拷贝构造 [nmat.h:L223-L234](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L223-L234) 与 `release` [nmat.h:L410-L432](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L410-L432)。`NCNN_XADD` 是原子加（裸机单核下退化为普通加，见第 4.3 节），保证多线程安全。这一机制让 `vector<ncnn::Mat>` 里大量拷贝 Mat 时不会重复深拷贝大块张量数据。

#### 4.2.4 代码实践

**实践：手算一个 Mat 的 cstep 与 total，并在消费侧核对**

1. 实践目标：用 yolov4-tiny 的真实输出 shape 验证你对 cstep 的理解。
2. 操作步骤：
   - 取 u5-l2 给出的输出 shape 之一：`[1, 255, 13, 13]`（NCHW），即 `c=255, h=13, w=13`，`elemsize=4`（float）。
   - 按上面公式手算 `cstep` 与 `total()`，其中 `total() = cstep * c`（见 [nmat.h:L439-L442](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L439-L442)）。
   - 对照 `epmat2nmat` 创建 Mat 的那行 `ncnn::Mat dstmat = ncnn::Mat(width, height, channel);`（[eeptpu_sa.cpp:L338](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L338)），确认它走的是 dims=3 分支。
3. 需要观察的现象：因为 169×4=676 不是 16 的倍数，cstep 会大于 169。
4. 预期结果：`cstep = alignSize(676,16)/4 = 688/4 = 172`；`total = 172 × 255 = 43860` 个 float（≈ 175 KB）。注意它略大于「逻辑元素数」255×169=43095，多出来的就是每平面 3 个 float 的对齐填充。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `channel(c)` 返回的 Mat 不会在析构时把整块 data 释放掉？

**参考答案**：`channel(c)` 调用的是 external-data 构造函数，该构造函数把 `refcount` 置为 0（[nmat.h:L256-L264](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L256-L264)）。而 `release()` 只在 `refcount` 非空且减到 0 时才 `fastFree`（[nmat.h:L410-L418](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L410-L418)）。refcount=0 视为「不持有」，故视图析构是空操作。

**练习 2**：把 `cstep` 直接定义成 `w*h`（不做 16 字节对齐）会有什么后果？

**参考答案**：每个通道平面的字节数 `w*h*4` 不再保证 16 的倍数，于是 `channel(c).data` 不一定 16 字节对齐；后续若用 NEON `vld1q.f32`（要求 16 字节对齐的 128 位加载）做向量化后处理，可能触发对齐异常或被迫降速。对齐填充就是为了避免这一点。

---

### 4.3 fastMalloc：带对齐与隐藏头指针的内存分配

#### 4.3.1 概念说明

C 标准库的 `malloc` 通常只保证返回 8 字节对齐（32 位）或 16 字节对齐（64 位），但**不保证**更大对齐。而 ncnn::Mat 需要 16 字节对齐的 `data`（NEON/SIMD、TPU 友好）。在没有 `aligned_alloc`/`posix_memalign` 的裸机环境里，怎么拿到一块「任意 N 字节对齐」的内存？

经典手法叫 **over-allocate + align + remember**：

1. 多申请一点（`size + sizeof(void*) + MALLOC_ALIGN`），保证里面一定有一段满足对齐的可用区。
2. 在这段内存里找到对齐地址 `adata`。
3. 把「真正的 malloc 起点 `udata`」藏到 `adata` 前面一个指针槽位（`adata[-1]`），这样将来 `fastFree` 还能找回原指针去 `free`。

#### 4.3.2 核心流程

```
malloc(size + sizeof(void*) + 16)
   │
   ▼
udata ───────────────────────────────────────────────►  (malloc 原始起点)
        │ <- +1 指针  ->│  ...  │<- 对齐到 16 -> adata   (返回给用户)
                                 adata[-1] = udata      (把原指针藏在 adata 前一格)
   │
   ▼
fastFree(ptr):  udata = ((void**)ptr)[-1]; free(udata);
```

用户拿到的 `ptr == adata` 一定 16 字节对齐；原指针藏在它前一个 `void*` 槽里，释放时取回即可。

#### 4.3.3 源码精读

```cpp
#define MALLOC_ALIGN    16

static inline void* fastMalloc(unsigned int size)
{
    unsigned char* udata = (unsigned char*)malloc(size + sizeof(void*) + MALLOC_ALIGN);
    if (!udata) return 0;
    unsigned char** adata = alignPtr((unsigned char**)udata + 1, MALLOC_ALIGN);
    adata[-1] = udata;     // 把真正的 malloc 指针藏在返回指针前一格
    return adata;
}

static inline void fastFree(void* ptr)
{
    if (ptr) {
        unsigned char* udata = ((unsigned char**)ptr)[-1];   // 取回原指针
        free(udata);
    }
}
```

参见 [nmat.h:L78-L96](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L78-L96)，对齐常数在 [nmat.h:L30-L31](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L30-L31)。逐行看几个细节：

- `+sizeof(void*)`：给「藏原指针」预留一个指针槽位（`adata[-1]`）。
- `+MALLOC_ALIGN`：给对齐预留最多 15 字节的滑动余量。
- `alignPtr(udata + 1, 16)`：从 `udata` 之后**至少一个指针**的位置开始往上找 16 字节对齐地址，保证 `adata[-1]` 不会越界写到 `udata` 之前（那里不属于本块）。
- `adata[-1] = udata`：巧妙利用「对齐地址前一格」存原指针，**无需额外元数据结构**。
- `fastFree` 读 `((void**)ptr)[-1]` 还原——所以 `fastMalloc` 的返回值**必须**成对地交给 `fastFree`，不能混用普通 `free`。

Mat 在 `create` 里正是用 `fastMalloc` 拿对齐内存，并把 `refcount` 放在该块尾部（[nmat.h:L394-L401](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L394-L401)），`release` 里用 `fastFree` 释放（[nmat.h:L412-L418](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L412-L418)）。另外，`nmat.h` 顶部那大段 `NCNN_XADD` 宏（[nmat.h:L41-L66](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L41-L66)）按编译器选择原子 intrinsic，最末的 `#else` 分支是裸机/无原子内建时的非线程安全兜底——单核裸机够用。

#### 4.3.4 代码实践

**实践：画出 fastMalloc 的内存布局**

1. 实践目标：把「超额申请 + 对齐 + 藏指针」在纸上具象化。
2. 操作步骤：假设 `size=100`、`sizeof(void*)=4`、`MALLOC_ALIGN=16`，且 `malloc` 返回的 `udata = 0x1000 0FF8`（注意它本身不是 16 对齐）。
   - 算出实际 malloc 字节数。
   - 用 `alignPtr(udata+4, 16)` 公式手算 `adata`，确认 `adata` 是 16 的倍数。
   - 标出 `adata[-1]` 指向哪个地址、存的是什么。
3. 需要观察的现象：`adata` 会落在 `0x10001000`（向上对齐到 16 的倍数），`adata[-1]` 落在 `0x10000FFC`，存放值 `0x10000FF8`。
4. 预期结果：你得到一张清晰的「udata / adata / adata[-1] / 用户可用区」四段布局图，并能解释为何多申请 `sizeof(void*)+16` 就一定够用（最多滑动 15 字节 + 1 个指针槽）。

#### 4.3.5 小练习与答案

**练习**：如果把 `fastMalloc` 的返回值误交给标准 `free` 而不是 `fastFree`，会发生什么？

**参考答案**：`free` 收到的是 `adata`（对齐后的地址），而真正 `malloc` 的起点是 `udata = adata[-1]`。把非 malloc 起点的指针交给 `free` 属于未定义行为，轻则堆元数据损坏、后续 `malloc/free` 崩溃，重则立即异常。因此 `fastMalloc/fastFree` 必须成对使用。

---

### 4.4 alignSize / alignPtr：幂二次方对齐的位运算数学

#### 4.4.1 概念说明

`alignPtr` 与 `alignSize` 是 nmat.h 的「地基」：前者把**指针/地址**向上对齐，后者把**大小**向上对齐，都要求对齐量 n 是 2 的幂。它们用同一个位运算套路实现「向上取整到 n 的倍数」，没有除法、没有分支，在裸机上又快又确定。

#### 4.4.2 核心流程与数学

对齐向上的直觉：找到「不小于 x 的、最小的 n 的倍数」。数学定义：

\[
\text{alignUp}(x, n) = \left\lceil \frac{x}{n} \right\rceil \cdot n
\]

当 n 是 2 的幂时，\(n-1\) 的二进制是连续的 1（例如 n=16 → n-1=15=`0b1111`），它正好是「块内偏移」的掩码。补码下 \(-n = \sim(n-1)\)，于是向上对齐可以一步位运算完成：

\[
\text{alignUp}(x, n) = (x + n - 1)\ \&\ \sim(n-1) = (x + n - 1)\ \&\ (-n)
\]

- 加 `n-1`：先把 x 推到「下一个块边界或当前块边界」。
- `& -n`（即 `& ~(n-1)`）：把低 \(\log_2 n\) 位清零，向下抹平到块边界。

两者合起来正是「向上取整到 n 的倍数」。注意这个公式**只在 n 为 2 的幂时成立**，否则 `-n` 不是干净的低位掩码——这也是 `MALLOC_ALIGN` 必须取 16/32 这类 2 的幂的原因。

#### 4.4.3 源码精读

```cpp
// 把指针向上对齐到 n 字节（n 为 2 的幂）
template<typename _Tp> static inline _Tp* alignPtr(_Tp* ptr, int n=(int)sizeof(_Tp))
{
    return (_Tp*)(((size_t)ptr + n-1) & -n);
}

// 把大小向上对齐到 n 的倍数（n 为 2 的幂）
static inline unsigned int alignSize(unsigned int sz, int n)
{
    return (sz + n-1) & -n;
}
```

参见 `alignPtr` [nmat.h:L33-L39](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L33-L39)、`alignSize` [nmat.h:L69-L76](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/nmat.h#L69-L76)。两者结构完全一样——一个是 `(size_t)ptr`，一个是 `unsigned int sz`。

另一个细节：本工程里还有一个**非幂二次方**的对齐宏 `round_up(x, y)`（[eep_interface.h:L46](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/interface/eep_interface.h#L46)），用 `(((x)-1)|__round_mask(x,y))+1` 实现，可对齐到任意正整数 y（如把通道数向上取整到 16）。它被 `epmat_get_size` 用来算 epmat 字节数 `h*w*round_up(c,16)*2`（[eeptpu_sa.cpp:L311-L312](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L311-L312)）。两者不要混淆：`alignSize/alignPtr` 走位运算（仅 2 的幂、极快），`round_up` 走掩码补码（任意 y、稍慢）。

**为什么 `MALLOC_ALIGN=16` 对 TPU 的 16 通道 epmat 友好？** 把这条因果链串起来：

1. TPU 的 epmat 以「16 通道 × 2 字节 = 32 字节」为最小访存单元（u4-l4 / u5-l2），整条数据通路都是围绕「16 通道」这个粒度设计的。
2. `epmat2nmat` 把 epmat 反量化成 `ncnn::Mat`，其 `data` 由 `fastMalloc` 分配 → 起点 16 字节对齐（`MALLOC_ALIGN=16`）。
3. 每个**通道平面**的字节大小经 `alignSize(...,16)` 向上取整到 16 的倍数（`cstep` 公式），于是 `channel(c).data` 在 c 任意取值下都保持 16 字节对齐。
4. 16 字节 = 128 位，恰好等于一条 ARM NEON 向量（`vld1q.f32` 一次装载 4 个 float）。这意味着 CPU 侧后处理（topk 遍历、yolo 解码、NMS）可以用**单条对齐向量加载**遍历每个通道平面，不存在跨对齐边界的惩罚。

简言之，`MALLOC_ALIGN=16` 不是拍脑袋的数字，而是让「TPU 的 16 通道硬件粒度 ↔ ncnn::Mat 的通道平面布局 ↔ ARM NEON 的 128 位向量宽度」三者在对齐上严丝合缝的粘合剂。若改成 8，通道平面就会落在 8 字节而非 16 字节边界，破坏上述第 4 步的单指令加载；改成 32 理论更贴近 32 字节的 epmat 单元，但会浪费更多 padding 且对 NEON（128 位）无额外收益——故 16 是「最小够用」的甜点。

#### 4.4.4 代码实践

**实践：用位运算手算对齐，并改 `MALLOC_ALIGN` 观察后果（实践任务的核心）**

1. 实践目标：吃透 \((x+n-1)\ \&\ (-n)\)，并口头推演修改对齐常量的影响。
2. 操作步骤：
   - 手算 `alignSize(676, 16)` 与 `alignSize(676, 32)`，验证前者=688、后者=704。
   - 手算 `alignPtr` 在 `ptr=0x10000FF8, n=16` 的结果（应为 `0x10001000`）。
   - **思考实验**（不要真改仓库源码）：若把 `MALLOC_ALIGN` 从 16 改为 8，重新算 13×13 float 张量的 `cstep`，并说明 `channel(c).data` 是否仍 16 字节对齐。
3. 需要观察的现象：`alignSize(676,8)=680 → cstep=170`；此时平面大小 680 字节虽是 8 的倍数，却不一定是 16 的倍数，多个平面累加后 `channel(c).data` 可能落在 8 字节而非 16 字节边界。
4. 预期结果：你能用一句话回答实践任务的两问——「`MALLOC_ALIGN=16` 友好是因为它让 Mat 每个通道平面起点都 16 字节对齐，正好匹配 ARM NEON 128 位向量和 TPU 16 通道粒度；simplestl 能替代 `std::vector` 是因为它在 `namespace std` 内提供了同名的 `vector/pair/string/partial_sort` 并补齐了全局 `operator new/delete`，使依赖标准库的推理代码零改动即可在无 libstdc++ 的裸机环境链接通过。」
5. 若无法本地运行（无 Vitis 工具链），明确标注「待本地验证」：可在 PC 上写一段最小 C++ 程序，`#include "nmat.h"` 后打印 `ncnn::Mat(13,13,255).data` 的地址对 `16` 取模是否为 0，以验证对齐结论。

#### 4.4.5 小练习与答案

**练习 1**：`alignSize(1000, 16)` 等于多少？写出推导。

**参考答案**：\(1000 + 15 = 1015\)，\(1015\ \&\ (-16) = 1015\ \&\ \text{0xFFFFFFF0}\)。1015 = `0b1111110111`，低 4 位 `0111`=7，清零得 `0b1111110000`=1008，再加回… 应直接为 1008？复核：ceil(1000/16)=63，63×16=1008。是的，`alignSize(1000,16)=1008`。

**练习 2**：为什么 `alignSize` 的注释强调「n must be a power of two」？

**参考答案**：公式 `(sz + n-1) & -n` 依赖 `-n == ~(n-1)` 且 `n-1` 是连续低 1 位掩码，这只在 n 为 2 的幂时成立。若 n 非 2 的幂（如 n=24），`-n` 不是干净掩码，结果错误；此时应改用 `round_up` 这类任意除数的对齐宏。

## 5. 综合实践

**任务：追踪一个浮点数，从 TPU epmat 到 partial_sort，把本讲四个模块串成一条线。**

参考 [eeptpu_sa.cpp:L330-L361](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L330-L361) 的 `epmat2nmat` 与 [eeptpu_sa.cpp:L364-L385](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/eeptpu/eeptpu_sa.cpp#L364-L385) 的 `read_forward_result`，按下列要求产出一张「数据流 + 基础设施」对照表：

1. **内存来自哪**：`read_forward_result` 里 `out = epmat2nmat(...)` 创建的 `ncnn::Mat`，其 `data` 由谁分配？（答：`Mat::create` → `fastMalloc`，16 字节对齐。）
2. **通道怎么写**：`epmat2nmat` 用 `dstmat.channel(c).data` 拿到第 c 个平面起点——这个起点为什么一定 16 字节对齐？（答：`data` 16 对齐 + `cstep` 经 `alignSize(...,16)` 保证每平面字节大小是 16 的倍数。）
3. **怎么收起来**：`outputs.push_back(out)` 把 Mat 塞进 `vector<ncnn::Mat>`——这个 `vector` 是标准库还是 simplestl？`push_back` 内部走的 `new char[]` 最终落到哪个函数？（答：simplestl 的 `vector`；`operator new` → `malloc`，见 [simplestl.cpp:L22-L26](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/simplestl.cpp#L22-L26)。）
4. **怎么排序**：分类路线把 Mat 喂给 [classify.cpp:L23-L47](https://github.com/embedeep/FREE-TPU-V3plus-for-FPGA/blob/1d3b64b6f0160680e6391105bdfb3739c0aba14a/sdk/standalone/src/post_process/classify.cpp#L23-L47) 的 `get_topk`，它用 `vector<pair<float,int>>` + simplestl 的 `partial_sort` + `greater`。请指出这三者各自的定义文件。

最终产物是一段说明文字，把「epmat(int16,32 字节步) → fastMalloc 对齐内存 → ncnn::Mat 通道视图 → simplestl vector 收集 → simplestl partial_sort 取 topk」这条链路上的每一步，标注它依赖本讲哪个最小模块（容器/张量/分配器/对齐数学）。这条链正好是 u5-l2 留下的「黑盒」的全部内部结构。

## 6. 本讲小结

- 裸机工程自带 `simplestl`，是因为 BSP 不保证完整 C++ 标准库；它在 `namespace std` 内补齐 `vector/string/pair/list/partial_sort/greater/less`，并在 `simplestl.cpp` 提供全局 `operator new/delete`（包到 `malloc/free`），让依赖标准库的推理代码零改动链接通过。
- `ncnn::Mat` 用 `dims/w/h/c/cstep` 描述张量，`cstep`（每通道平面**元素**步长）在 dims=3 时经 `alignSize(w*h*elemsize,16)/elemsize` 计算，保证每平面 16 字节对齐；`channel(c)` 返回零拷贝视图，是 `epmat2nmat` 逐通道写入浮点的接口。
- `fastMalloc` 用「超额申请 + `alignPtr` 对齐 + `adata[-1]` 藏原指针」拿到任意 2 的幂对齐内存，必须与 `fastFree` 成对使用；Mat 的引用计数靠 `NCNN_XADD`（裸机退化为非原子加）实现共享。
- `alignSize`/`alignPtr` 用 \((x+n-1)\ \&\ (-n)\) 做无分支向上对齐，仅适用于 2 的幂；与任意除数的 `round_up` 宏区分开。
- `MALLOC_ALIGN=16` 是粘合剂：让 TPU 的 16 通道硬件粒度、Mat 的通道平面对齐、ARM NEON 128 位向量三者严丝合缝，使 CPU 侧后处理可单指令对齐加载。

## 7. 下一步学习建议

- **横向对比 ncnn 原版**：本工程的 `nmat.h` 只是 ncnn `mat.h` 的子集。建议去 ncnn 开源仓库对照原版 `Mat`，看看本工程删掉了哪些（如 `Allocator` 的多种后端、`Mat::substract_mean_normalize`、`copy_make_*`），理解「为什么裸机版只留这些」。
- **回看消费侧**：结合 u6-l1（分类 topk）与 u6-l3（yolo 软件后处理），观察后处理算子如何依赖 Mat 的 `channel()`/`cstep` 布局；这会反向加深你对 cstep 对齐的理解。
- **移植实践**：尝试把这套 `simplestl` + `nmat.h` 拷到一个最小 ARM 裸机工程，只保留 `vector`/`Mat`/`fastMalloc`，写一段「分配一个 `[1,3,416,416]` 的 Mat 并逐通道填值」的代码，验证对齐与引用计数行为——这正是 u8-l4（性能、精度与移植实践）要做的移植基本功。
- **延伸阅读**：NEON intrinsics（`vld1q_f32`/`vst1q_f32`）与 C11 `aligned_alloc`，理解「对齐内存」在不同平台/语言版本下的标准做法，体会 `fastMalloc` 这一旧式手法的历史定位。

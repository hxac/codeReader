# 用 C++ 和 C API 直接调用算子

## 1. 本讲目标

学完本讲,你应该能够:

1. 不借助 Python,分别用 `cvcuda::Flip` C++ 类和 `cvcudaFlipCreate/cvcudaFlipSubmit` 纯 C 函数完成一次完整的 flip 调用。
2. 说清 C API 的句柄式生命周期:Create 拿句柄 → Submit 用句柄 → Destroy 还句柄,以及每一步失败时会发生什么。
3. 对照 `OpFlip.hpp` 里不到 20 行的内联包装,读懂 `OpFlip.h` 里 C API 的「薄封装」模式——理解为什么这两份文件长得如此对称。
4. 掌握 C 与 C++ 两侧各自的错误处理写法:返回码 `NVCVStatus` 与异常 `nvcv::Exception` 的边界互译。

本讲承接 u5-l1 的四层结构。u5-l1 是「自顶向下读一遍」flip 的调用链;本讲把视角反过来:作为 **C/C++ 应用开发者**,你要亲手把这条链的最上面两层用起来。

## 2. 前置知识

本讲假设你已读过 u5-l1(算子四层结构)。以下几个概念用通俗语言再过一遍:

- **句柄(handle)**:一个不透明的指针。C 语言没有类,`cvcudaFlipCreate` 无法返回一个 `cvcuda::Flip` 对象,于是返回 `NVCVOperatorHandle`——你只知道「它是算子的钥匙」,不知道钥匙后面是什么。真身其实是 priv 层的 C++ 对象指针(见 4.3 节的 `reinterpret_cast` 技巧)。
- **RAII(Resource Acquisition Is Initialization)**:C++ 的资源管理惯用法——资源在构造函数里获取、在析构函数里释放。栈对象离开作用域时析构自动执行,所以「忘记释放」这类 bug 在 RAII 下天然消失。
- **`extern "C"` 与 C ABI**:C++ 编译器会给函数名「改名」(name mangling)以支持重载,C 编译器不会。`extern "C"` 告诉 C++ 编译器「这个符号按 C 规则命名」,这使 libcvcuda.so 的导出符号在不同编译器、不同语言间稳定,是二进制兼容的基石。
- **异常边界**:C++ 异常穿越 `extern "C"` 边界是未定义行为。所以每个 C API 函数内部都要把异常「翻译」成错误码再返回——这正是本讲反复出现的 `ProtectCall` 的职责(u6-l2 会展开讲符号版本化)。
- **`NVCVStatus`**:整型错误码枚举,`NVCV_SUCCESS = 0`,其余值表示各类失败。C 侧唯一的错误通道。
- **引用计数句柄**:nvcv 的张量句柄不是「Create/Destroy」式,而是共享式——`nvcvTensorIncRef`/`nvcvTensorDecRef` 增减引用,归零自动销毁。算子句柄与张量句柄的生命周期模型**不同**,这是本讲一个容易踩的坑。

## 3. 本讲源码地图

| 文件 | 层 | 作用 |
|------|----|------|
| `src/cvcuda/include/cvcuda/OpFlip.h` | 公开 C API | Flip 的三个 C 函数声明 + Limitations 支持矩阵,`extern "C"` |
| `src/cvcuda/include/cvcuda/OpFlip.hpp` | 公开 C++ API | `cvcuda::Flip` 类声明 + 全内联实现,逐行转发到 C 函数 |
| `src/cvcuda/include/cvcuda/Operator.h` | 公开 C API | 算子句柄类型定义与统一销毁函数 `nvcvOperatorDestroy` |
| `src/cvcuda/include/cvcuda/IOperator.hpp` | 公开 C++ API | `IOperator` 抽象接口与 `detail::OperatorHandle` RAII 包装 |
| `src/cvcuda/OpFlip.cpp` | C API 实现 | `cvcudaFlipCreate/Submit/VarShapeSubmit` 的真身(`CVCUDA_DEFINE_API`) |
| `src/cvcuda/priv/IOperator.hpp` | 私有 | 句柄↔C++ 对象的互转:`CreateOperatorHandle`/`ToDynamicRef` |
| `src/nvcv/src/include/nvcv/Tensor.h` | nvcv C API | 纯 C 程序创建/导出/释放张量所需的全部函数 |
| `tests/cvcuda/system/TestOpFlip.cpp` | 测试 | C++ 类与 C 函数的官方用法范本 |

## 4. 核心概念与源码讲解

### 4.1 双面孔:每个算子都有 `.h` 与 `.hpp` 两份定义

#### 4.1.1 概念说明

CV-CUDA 对外暴露**两套等价的公开 API**:

- `.h` 文件(如 `OpFlip.h`):纯 C 接口。函数、句柄、错误码,零 C++ 特性,任何能调 C 共享库的语言(C、Rust、Fortran、 ctypes…)都能用。
- `.hpp` 文件(如 `OpFlip.hpp`):C++ 类(`cvcuda::Flip`),RAII 管理句柄、异常报告错误、成员函数调用。

为什么维护两套?因为 **C ABI 是最稳定的公共契约**:C++ 标准不保证 ABI 兼容(不同编译器版本、不同标准库的类布局可能不同),而 C 符号一旦定下就几乎不会再变。所以仓库的真实现实是:priv 层用 C++ 写,`src/cvcuda/OpXxx.cpp` 把它包成 C 函数(这一步由 `ProtectCall` 挡住异常),`.hpp` 再把 C 函数包回优雅的 C++ 类。**C++ 类只是 C API 的一层薄糖**,两层之间没有任何额外逻辑——记住这一点,两个文件就都能互相推导着读。

#### 4.1.2 核心流程

每个算子的 `.h` 文件遵循统一命名律:

```
cvcudaXxxCreate(...)          → 创建算子,返回 NVCVOperatorHandle
cvcudaXxxSubmit(...)          → 把算子提交到 cudaStream_t 上执行(规则 Tensor 批)
cvcudaXxxVarShapeSubmit(...)  → 变长批(ImageBatchVarShape)版本,部分算子没有
nvcvOperatorDestroy(handle)   → 所有算子共用的销毁函数(在 Operator.h,不在 OpXxx.h)
```

与之对称,`.hpp` 里:

```
cvcuda::Xxx 类 : 继承 IOperator
  ├─ 构造函数      → 调 cvcudaXxxCreate
  ├─ operator()    → 调 cvcudaXxxSubmit / cvcudaXxxVarShapeSubmit(按参数类型重载)
  └─ handle()      → 暴露底层 C 句柄
```

#### 4.1.3 源码精读

先看 C 侧。`OpFlip.h` 定义了三个函数,创建函数只带一个业务参数:

- [src/cvcuda/include/cvcuda/OpFlip.h:54](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L54):`cvcudaFlipCreate(NVCVOperatorHandle *handle, int32_t maxVarShapeBatchSize)`——算子在**创建期**就要知道变长批的最大容量(不用变长批时传 0),这与 Python 侧「构造算子对象」对应。
- [src/cvcuda/include/cvcuda/OpFlip.h:117-L118](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L117-L118):`cvcudaFlipSubmit(handle, stream, in, out, flipCode)`——注意参数是 `NVCVTensorHandle` 而非指针本体:执行期传数据、传流、传业务参数。
- [src/cvcuda/include/cvcuda/OpFlip.h:181-L183](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L181-L183):`cvcudaFlipVarShapeSubmit`——变长批版本,注意 `flipCode` 在这里升级成了 `NVCVTensorHandle`(每张图一个翻转码,u3-l1 讲过这个设计)。

`OpFlip.h` 里占篇幅最大的其实是注释:[OpFlip.h:56-101](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L56-L101) 的 **Limitations 契约表**逐项写明 Submit 支持的布局(NHWC/HWC/NCHW/CHW)、通道数(1/3/4)、每种 dtype 的取舍,以及「输出必须与输入同布局同形状」的依赖关系。写 C/C++ 程序前查这张表,和 Python 侧习惯一致。

再看 C++ 侧的类声明,它就是 C API 的镜像:

- [src/cvcuda/include/cvcuda/OpFlip.hpp:43-L56](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.hpp#L43-L56):`class Flip final : public IOperator`——两个 `operator()` 重载恰好对应 `Submit` 与 `VarShapeSubmit`;唯一的成员变量是 `detail::OperatorHandle m_handle`。

#### 4.1.4 代码实践

**实践目标**:验证「`.h`/`.hpp` 成对出现且命名对称」是全仓库规律,而不只 Flip 一家。

**操作步骤**:

1. 在仓库根目录执行:
   ```bash
   ls src/cvcuda/include/cvcuda/ | grep -c '\.h$'
   ls src/cvcuda/include/cvcuda/ | grep '\.hpp$' | wc -l
   ```
2. 任选三个算子头文件(如 `OpCvtColor`、`OpResize`、`OpOSD`),用 `rg "cvcuda\w+Create" src/cvcuda/include/cvcuda/OpCvtColor.h` 等查它们的 Create 函数名。
3. 找反例:是否存在只有 `.h` 没有 `.hpp` 的算子?(提示:`ls` 两个列表做差集,或直接数文件。)

**需要观察的现象**:`.h` 与 `.hpp` 数量几乎一一对应;每个 Create 函数名都严格是 `cvcuda` + 算子名 + `Create`。

**预期结果**:能列出一张家用的「算子名 → C 函数前缀 → C++ 类名」对照表,例如 `Flip → cvcudaFlip* → cvcuda::Flip`。数量是否严格相等需以你机器上的输出为准(待本地验证;仓库中存在少量仅有 C API 的历史算子)。

#### 4.1.5 小练习与答案

**练习 1**:`cvcudaFlipCreate` 为什么把句柄写成输出参数 `NVCVOperatorHandle *handle`,而不是返回值?

**答案**:返回值位置被 `NVCVStatus` 占用了。C API 的统一约定是「返回值只报告成败,数据一律经参数写出」,这样调用方不可能忘记检查错误(详见 4.3.3 与 u6-l2)。

**练习 2**:用 `OpFlip.h` 的 Limitations 表回答:一个 `int16`(16bit Signed)的 HWC 张量能否走 `cvcudaFlipSubmit`?

**答案**:不能。表中标量版 Submit 的 Input 表里 `16bit Signed | No`。但注意 [OpFlip.h:120-154](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.h#L120-L154) 的 **VarShape 版契约里 16bit Signed 是 Yes**——两份表并不相同,查表必须查到对应入口的那一份。

**练习 3**:`nvcvOperatorDestroy` 为什么不叫 `cvcudaFlipDestroy` 放在 `OpFlip.h` 里?

**答案**:因为销毁只依赖「它是个算子句柄」这一事实,与具体算子无关。放在 [Operator.h:36](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/Operator.h#L36) 一处定义、全算子共用,避免 61 个头文件各写一份等价声明。

### 4.2 C++ 类的 RAII 生命周期:`cvcuda::Flip` 与 `OperatorHandle`

#### 4.2.1 概念说明

用 C++ 类时你**不需要**手动调 Destroy——这是 `.hpp` 层给你的核心礼物。`cvcuda::Flip` 的全部秘密在 [IOperator.hpp](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/IOperator.hpp) 里的 `detail::OperatorHandle`:一个 move-only 的 RAII 包装,析构时恰好调用一次 `nvcvOperatorDestroy`。注释原话:拥有算子的包装类持有它之后,「就不必自己写任何 rule-of-five 样板」——拷贝删除、移动转移、析构释放,三件事全包了。

上层的 `IOperator` 抽象类则立了两条规矩:不可拷贝(防止两个 C++ 对象争抢同一个底层句柄、双重销毁),以及纯虚 `handle()`——任何算子类都必须能交出它的 C 句柄,这保证了 C++ 世界与 C 世界随时互通。

#### 4.2.2 核心流程

一次完整的 C++ 侧生命周期:

```
cvcuda::Flip flip;                      ── 构造:调 cvcudaFlipCreate,失败抛 nvcv::Exception
flip(stream, inTensor, outTensor, 1);   ── 调用:调 cvcudaFlipSubmit,失败抛异常
                                        ── (异步!kernel 只是入队,见 u4-l1)
cudaStreamSynchronize(stream);          ── 你自己同步,读结果
}                                       ── 离开作用域:析构 → nvcvOperatorDestroy 恰好一次
```

两条铁律:

1. **算子可以在同步之前析构**。句柄销毁的是「算子对象」,不会打断流上已排队的工作。
2. **张量绝不能在同步之前析构**。数据还在被排队中的 kernel 使用,这是流语义(u4-l1)决定的,与 RAII 无关。

#### 4.2.3 源码精读

构造函数——三行讲完「C++ 如何安全地包住 C」:

- [src/cvcuda/include/cvcuda/OpFlip.hpp:58-L64](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.hpp#L58-L64):先声明裸句柄 `h = nullptr`,把 `cvcudaFlipCreate` 的返回码交给 `CheckThrow`(失败即抛 `nvcv::Exception`),成功后才移交 `OperatorHandle` 接管。**先检查、后接管**的顺序保证了异常路径上不会误销毁句柄。

调用算子——纯转发:

- [src/cvcuda/include/cvcuda/OpFlip.hpp:66-L70](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.hpp#L66-L70):`operator()` 把 `in.handle()`/`out.handle()` 传给 `cvcudaFlipSubmit`,返回码照例过 `CheckThrow`。注意 `nvcv::Tensor::handle()` 在这里完成了「C++ 张量对象 → C 句柄」的降级。
- [src/cvcuda/include/cvcuda/OpFlip.hpp:72-L77](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.hpp#L72-L77):变长批重载,转发目标换成 `cvcudaFlipVarShapeSubmit`。

RAII 核心——`detail::OperatorHandle`:

- [src/cvcuda/include/cvcuda/IOperator.hpp:47-L53](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/IOperator.hpp#L47-L53):析构调用 `nvcvOperatorDestroy`(对 nullptr 调用它也是安全的,C API 内部会判空),拷贝构造/赋值 `= delete`——这是「不可拷贝」的机器执行层。
- [src/cvcuda/include/cvcuda/IOperator.hpp:55-L70](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/IOperator.hpp#L55-L70):移动构造/赋值——把源对象的句柄「偷」过来并把源置空,保证任一时刻只有一个包装者拥有句柄。
- [src/cvcuda/include/cvcuda/IOperator.hpp:83-L97](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/IOperator.hpp#L83-L97):`IOperator` 接口本体。注意 90–92 行的注释:C++ 移动语义「默认正确」的前提是基类**不携带任何状态**;若未来往基类加数据成员,必须重新审视移动语义——这是一个很值得学习的防御性注释。

官方用法范本(真实测试代码):

- [tests/cvcuda/system/TestOpFlip.cpp:122-L123](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L122-L123):`cvcuda::Flip flipOp;` 后直接 `flipOp(stream, inTensor, outTensor, flipCode)`,随后 `cudaStreamSynchronize`——教科书式的「构造-调用-同步」三步。
- [tests/cvcuda/system/TestOpFlip.cpp:204-L206](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L204-L206):变长批场景下构造函数传入批容量 `cvcuda::Flip flipOp(batches)`。

#### 4.2.4 代码实践

**实践目标**:体会「错误在 C++ 侧变成异常」以及 RAII 的自动清理。

**操作步骤**(示例代码,基于 TestOpFlip.cpp 的模式缩写):

```cpp
// 示例代码:错误路径实验(不是仓库文件)
#include <cvcuda/OpFlip.hpp>
#include <cstdio>

int main()
{
    try
    {
        cvcuda::Flip flip;              // maxVarShapeBatchSize 缺省为 0
        // 故意不创建流、不创建张量,直接看异常路径:
        // 下面这行用 nullptr 流提交 —— 待本地验证具体抛出的状态码
        // flip(0, in, out, 1);
    }
    catch (const nvcv::Exception &e)
    {
        std::printf("caught: %s\n", e.what());
    }
}   // flip 在这里自动析构,无需手动 Destroy
```

1. 把上面骨架补全(加上 u5-l2 讲过的张量创建),跑通一次正常 flip。
2. 再故意把输出张量换成错误 dtype(如 `FMT_RGBf32` 配 `U8` 输入),观察异常信息。

**需要观察的现象**:异常被 `catch` 捕获,`e.what()` 带出 priv 层的中文之外的可读信息(如 "Input must be cuda-accessible..." 一类);程序正常退出,无泄漏、无 double-free。

**预期结果**:正常路径输出翻转图;错误路径打印异常后干净退出。具体错误消息文本待本地验证。

#### 4.2.5 小练习与答案

**练习 1**:`cvcuda::Flip` 为什么标记 `final` 且没有虚析构以外的新虚函数?

**答案**:`IOperator` 已定义唯一虚接口 `handle()`;算子类只是「实现 + 持有句柄」的具体类型,没有再派生的需求,`final` 阻止误继承、也给编译器去虚化的机会。

**练习 2**:下面的代码有什么隐患?

```cpp
auto *flip = new cvcuda::Flip();
flip->(stream, in, out, 1);   // 语法先不管
delete flip;
```

**答案**:功能上没错(RAII 在 `delete` 时仍会触发),但裸 `new/delete` 让「忘记 delete」重新成为可能,等于主动放弃了 RAII 的价值。正确写法是栈对象或 `std::unique_ptr<cvcuda::Flip>`(类已不可拷贝但可移动,配合智能指针无障碍)。

**练习 3**:[OpFlip.hpp:79-82](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/include/cvcuda/OpFlip.hpp#L79-L82) 的 `Flip::handle()` 什么场景下会用到?

**答案**:需要跨回 C 世界时——比如你的程序主体用 C++,却要把算子交给某个只认 `NVCVOperatorHandle` 的 C 接口回调。`handle()` 就是 C++→C 的逃生门。

### 4.3 纯 C API:Create/Submit/Destroy 与薄封装的真身

#### 4.3.1 概念说明

纯 C 调用意味着放弃 RAII 和异常,换回三样东西:**手动生命周期**、**返回码检查**、**句柄穿针引线**。值得学的原因有二:其一,某些集成环境(嵌入式 C 代码、其他语言的 FFI)只有这条路;其二,读 C API 实现能看清仓库的「异常边界」工程技术——每个 `cvcuda*` 函数都是同一个模板的实例。

注意算子句柄与张量句柄生命周期模型不同:
- 算子:`nvcvOperatorDestroy(handle)` 一击必杀。
- 张量:引用计数,`nvcvTensorDecRef(handle, NULL)` 减一,归零自动销毁。C++ 侧的 `nvcv::Tensor` 析构时做的正是这件事。

#### 4.3.2 核心流程

纯 C 完成 flip 的完整时序:

```
1. nvcvTensorCalcRequirementsForImages(1, W, H, NVCV_IMAGE_FORMAT_RGB8, 0,0, &reqs)
2. nvcvTensorConstruct(&reqs, NULL, &in);  同样地建 out      ← NULL = 默认分配器
3. nvcvTensorExportData(in, &inData)                        ← 拿 basePtr 供 cudaMemcpy
4. cudaMemcpy(inData.buffer.strided.basePtr, hostPix, ..., cudaMemcpyHostToDevice)
5. cudaStreamCreate(&stream)
6. cvcudaFlipCreate(&op, 0)                                  ← 检查返回码!
7. cvcudaFlipSubmit(op, stream, in, out, 1)                  ← 检查返回码!异步入队
8. cudaStreamSynchronize(stream)
9. cudaMemcpy(HostToDevice 反向) 读回 out
10. nvcvTensorDecRef(in/out), nvcvOperatorDestroy(op), cudaStreamDestroy(stream)
```

#### 4.3.3 源码精读

C 函数的真身在 `src/cvcuda/OpFlip.cpp`(不是 priv/OpFlip.cpp——那是 priv 类的实现,u5-l1 讲过):

- [src/cvcuda/OpFlip.cpp:30-L43](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp#L30-L43):`cvcudaFlipCreate`。三个动作:(a) 整个函数体包进 `nvcv::ProtectCall`;(b) 判空 `handle` 抛异常(异常会被 ProtectCall 转成 `NVCV_ERROR_INVALID_ARGUMENT` 返回);(c) `priv::CreateOperatorHandle<priv::Flip>(...)` 造出 priv 对象并把指针 reinterpret 成 C 句柄。**创建期不碰 CUDA,只装配 C++ 对象**,所以很快。
- [src/cvcuda/OpFlip.cpp:45-L57](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp#L45-L57):`cvcudaFlipSubmit`。`TensorWrapHandle` 把 C 张量句柄包成**不增引用**的 C++ 视图(u5-l1 讲过的 NonOwningResource 技巧,避免跨边界 incRef/decRef 开销),然后 `ToDynamicRef<priv::Flip>(handle)` 还原类型并调用。
- [src/cvcuda/OpFlip.cpp:59-L72](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/OpFlip.cpp#L59-L72):`cvcudaFlipVarShapeSubmit`,同构的第三份。

句柄还原处的两级防御(私有层):

- [src/cvcuda/priv/IOperator.hpp:73-L89](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/IOperator.hpp#L73-L89):`ToDynamicRef` 先判空(NULL → `ERROR_INVALID_ARGUMENT` "Handle cannot be NULL"),再 `dynamic_cast` 验类型(把 Resize 的句柄传给 Flip 的 Submit → `ERROR_NOT_COMPATIBLE` "doesn't correspond to the requested object or was already destroyed")。**C API 用户传错句柄不会段错误,而是拿到明确错误码**。
- [src/cvcuda/priv/IOperator.hpp:54-L58](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/IOperator.hpp#L54-L58):`CreateOperatorHandle` 的 `reinterpret_cast<NVCVOperatorHandle>(op.release())`——「不透明句柄」的全部魔法就这一行:C++ 对象指针直接当 C 指针发还。

异常↔错误码的两座桥:

- [src/nvcv/src/include/nvcv/Exception.hpp:243-L256](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Exception.hpp#L243-L256):`ProtectCall`——执行 lambda,无异常返回 `NVCV_SUCCESS`;有异常则把异常内容写进线程局部错误槽,返回对应 `NVCVStatus`。这是 **C++→C** 方向。
- [src/nvcv/src/include/nvcv/detail/CheckError.hpp:43-L50](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/detail/CheckError.hpp#L43-L50):`CheckThrow`——状态非 0 就从错误槽取出消息抛 `nvcv::Exception`。这是 **C→C++** 方向(`OpFlip.hpp` 全靠它)。

纯 C 侧的张量工具(都在 `nvcv/Tensor.h`):

- [src/nvcv/src/include/nvcv/Tensor.h:43](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.h#L43):`typedef struct NVCVTensor *NVCVTensorHandle;`——又一个不透明指针。
- [src/nvcv/src/include/nvcv/Tensor.h:138-L140](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.h#L138-L140):`nvcvTensorCalcRequirementsForImages`——C 侧创建张量的第一步永远是「算需求」(u6-l3 会讲 Requirements 协商)。
- [src/nvcv/src/include/nvcv/Tensor.h:160-L161](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.h#L160-L161):`nvcvTensorConstruct(&reqs, alloc, &handle)`——第二步「按需构造」,`alloc` 传 NULL 用默认分配器。
- [src/nvcv/src/include/nvcv/Tensor.h:350](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.h#L350) 与 [src/nvcv/src/include/nvcv/Tensor.h:241](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Tensor.h#L241):`nvcvTensorExportData` 拿数据指针(basePtr 在 [TensorData.h:31-L40](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/TensorData.h#L31-L40) 的 `buffer.strided` 联合成员里),`nvcvTensorDecRef` 释放。

官方负向测试范本:

- [tests/cvcuda/system/TestOpFlip.cpp:468-L471](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/tests/cvcuda/system/TestOpFlip.cpp#L468-L471):`EXPECT_EQ(cvcudaFlipCreate(nullptr, 2), NVCV_ERROR_INVALID_ARGUMENT);`——C API 的判空契约有测试钉死。

#### 4.3.4 代码实践

**实践目标**:体验「C API 失败不抛异常、只给返回码」,并学会取出人类可读的错误消息。

**操作步骤**(示例代码):

```c
/* 示例代码:错误路径实验(不是仓库文件) */
#include <cvcuda/OpFlip.h>
#include <nvcv/Status.h>
#include <stdio.h>

int main(void)
{
    NVCVOperatorHandle op = NULL;
    NVCVStatus st = cvcudaFlipCreate(NULL, 0);   /* 故意传 NULL 输出参数 */
    printf("create(NULL) -> %d\n", (int)st);

    char msg[256];
    nvcvGetLastErrorMessage(msg, sizeof msg);    /* 错误消息在线程局部槽里 */
    printf("message: %s\n", msg);

    st = cvcudaFlipCreate(&op, 0);
    if (st != NVCV_SUCCESS) return 1;
    nvcvOperatorDestroy(op);                     /* 用完即毁,不依赖任何 GC */
    return 0;
}
```

**需要观察的现象**:第一处调用打印非 0 状态码(`NVCV_ERROR_INVALID_ARGUMENT`),`msg` 里是 "Pointer to NVCVOperator handle must not be NULL"(正是 OpFlip.cpp:36-38 抛的那句,经 ProtectCall 转录);第二处成功后程序干净退出。

**预期结果**:状态码数值可对照 [src/nvcv/src/include/nvcv/Status.h](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/Status.h#L50) 的枚举表。具体数值与消息文本待本地验证。

#### 4.3.5 小练习与答案

**练习 1**:把 `cvcudaResizeSubmit` 的句柄误传给 `cvcudaFlipSubmit`,会发生什么?依据是哪段代码?

**答案**:不会崩溃。`ToDynamicRef<priv::Flip>` 的 `dynamic_cast` 失败,抛出 `ERROR_NOT_COMPATIBLE`,被 `ProtectCall` 转成返回码。依据 [priv/IOperator.hpp:80-88](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/cvcuda/priv/IOperator.hpp#L80-L88)。

**练习 2**:为什么 `TensorWrapHandle` 要特意做成「不增引用」的包装,而不是直接 `Tensor::FromHandle(h, true)`?

**答案**:Submit 只是借用张量去启动 kernel,不延长其生命周期——引用关系由调用方的句柄/流语义保证。省掉每次 Submit 的 incRef/decRef 原子操作,热路径零开销。设计动机写在 [CoreResource.hpp:221-L243](https://github.com/CVCUDA/CV-CUDA/blob/5ac8708bde57f8cf1c8d19443f6384875e86157c/src/nvcv/src/include/nvcv/CoreResource.hpp#L221-L243) 的注释里。

**练习 3**:纯 C 程序里,第 8 步 `cudaStreamSynchronize` 之前能不能先 `nvcvTensorDecRef(in)`?

**答案**:不能。kernel 还排在流上没执行完,输入张量的显存可能仍将被读取;引用计数归零会立刻释放显存,产生未定义行为。正确顺序是:同步 → 读回 → 再减引用。(对照 4.2.2 的「两条铁律」,C 与 C++ 侧约束相同,只是 C 侧没有 RAII 替你兜底。)

## 5. 综合实践

**任务**:写两个各约 30–40 行的等价程序,对同一张程序生成的渐变图做左右镜像(`flipCode = 1`),各自保存为 PPM 图片,然后逐字节比对确认输出一致。

- **程序 A(C++ 类版)**:用 `nvcv::Tensor` + `cvcuda::Flip`,全程无手动 Destroy。
- **程序 B(纯 C 版)**:只用 `nvcvTensor*` C 函数 + `cvcudaFlipCreate/Submit` + `nvcvOperatorDestroy`,不 include 任何 `.hpp`。

**准备**:输入用 R 通道沿宽度、G 通道沿高度渐变的 RGB8 图(尺寸建议 64×32),这样镜像后肉眼即可验证;输出写成二进制 PPM(`P6\nW H\n255\n` + 像素),无需任何图像库。

**程序 A 参考骨架(示例代码)**:

```cpp
// 示例代码:flip_cpp.cpp —— C++ 类版(不是仓库文件)
#include <cvcuda/OpFlip.hpp>
#include <nvcv/ImageFormat.hpp>
#include <nvcv/Tensor.hpp>
#include <cuda_runtime.h>
#include <cstdint>
#include <cstdio>
#include <vector>

int main()
{
    const int W = 64, H = 32, C = 3;
    std::vector<uint8_t> pix(W * H * C);
    for (int y = 0; y < H; ++y)
        for (int x = 0; x < W; ++x)
        {
            pix[(y * W + x) * C + 0] = (uint8_t)(x * 255 / (W - 1)); // R:横向渐变
            pix[(y * W + x) * C + 1] = (uint8_t)(y * 255 / (H - 1)); // G:纵向渐变
            pix[(y * W + x) * C + 2] = 0;
        }

    nvcv::Tensor in(1, nvcv::Size2D{W, H}, nvcv::FMT_RGB8);   // Tensor.hpp:173 的图像构造器
    nvcv::Tensor out(1, nvcv::Size2D{W, H}, nvcv::FMT_RGB8);
    auto inBuf  = in.exportData<nvcv::TensorDataStridedCuda>().value();
    auto outBuf = out.exportData<nvcv::TensorDataStridedCuda>().value();

    cudaStream_t stream;
    cudaStreamCreate(&stream);
    cudaMemcpyAsync(inBuf.basePtr(), pix.data(), pix.size(), cudaMemcpyHostToDevice, stream);

    cvcuda::Flip flip;                                        // 创建算子
    flip(stream, in, out, 1);                                 // 左右镜像,异步入队

    std::vector<uint8_t> res(pix.size());
    cudaMemcpyAsync(res.data(), outBuf.basePtr(), pix.size(), cudaMemcpyDeviceToHost, stream);
    cudaStreamSynchronize(stream);                            // 张量在同步之后才析构!

    std::FILE *f = std::fopen("flip_cpp.ppm", "wb");
    std::fprintf(f, "P6\n%d %d\n255\n", W, H);
    std::fwrite(res.data(), 1, res.size(), f);
    std::fclose(f);
}   // flip/in/out 离开作用域自动释放,零手动清理
```

**程序 B 参考骨架(示例代码)**:

```c
/* 示例代码:flip_c.c —— 纯 C 版(不是仓库文件),生成输入与写 PPM 部分同上,从略 */
#include <cvcuda/OpFlip.h>
#include <nvcv/ImageFormat.h>
#include <nvcv/Tensor.h>
#include <cuda_runtime.h>
#include <stdio.h>

int run_flip(const uint8_t *hostPix, int W, int H, uint8_t *hostOut) /* 返回 0 成功 */
{
    NVCVTensorRequirements reqs;
    NVCVTensorHandle tin = NULL, tout = NULL;
    NVCVTensorData tdata;
    NVCVOperatorHandle op = NULL;
    cudaStream_t stream;
    int rc = -1, ok = 1;

    ok = ok && nvcvTensorCalcRequirementsForImages(1, W, H, NVCV_IMAGE_FORMAT_RGB8,
                                                   0, 0, &reqs) == NVCV_SUCCESS;
    ok = ok && nvcvTensorConstruct(&reqs, NULL, &tin) == NVCV_SUCCESS;   /* NULL=默认分配器 */
    ok = ok && nvcvTensorConstruct(&reqs, NULL, &tout) == NVCV_SUCCESS;
    ok = ok && nvcvTensorExportData(tin, &tdata) == NVCV_SUCCESS;
    if (!ok) goto done;

    size_t bytes = (size_t)W * H * 3;
    cudaStreamCreate(&stream);
    cudaMemcpyAsync(tdata.buffer.strided.basePtr, hostPix, bytes, cudaMemcpyHostToDevice, stream);

    if (cvcudaFlipCreate(&op, 0) != NVCV_SUCCESS) goto done;            /* 句柄式创建 */
    if (cvcudaFlipSubmit(op, stream, tin, tout, 1) != NVCV_SUCCESS) goto done;

    nvcvTensorExportData(tout, &tdata);
    cudaMemcpyAsync(hostOut, tdata.buffer.strided.basePtr, bytes, cudaMemcpyDeviceToHost, stream);
    cudaStreamSynchronize(stream);
    rc = 0;
done:
    if (op) nvcvOperatorDestroy(op);                                    /* 算子:Destroy */
    if (tin) nvcvTensorDecRef(tin, NULL);                               /* 张量:减引用 */
    if (tout) nvcvTensorDecRef(tout, NULL);
    if (stream) cudaStreamDestroy(stream);
    return rc;
}
```

**编译**(头文件与库路径按 u1-l3 的构建产物调整;若用 pip 安装的 wheel,头文件在 site-packages 下,待本地验证):

```bash
g++ -std=c++17 flip_cpp.cpp -o flip_cpp -I<build>/include -I<cuda>/include \
    -L<build>/lib -lcvcuda -lnvcv_types -lcudart
gcc -std=c11 flip_c.c -o flip_c $(同上路径与库)
```

**验收**:

1. `cmp flip_cpp.ppm flip_c.ppm` 无输出(逐字节相同);若不同,先怀疑两家程序生成输入的循环写得不一致,再怀疑业务逻辑。
2. 打开 PPM:R 渐变方向应左右翻转,G 渐变不变。
3. 用 `diff <(./flip_cpp && md5sum flip_cpp.ppm) <(./flip_c && md5sum flip_c.ppm)` 之类方式固化成一条自检命令。
4. 观察两版代码行数与清理代码量的对比——这是本讲主题最好的注脚:**同一功能,C++ 版把生命周期交给类型系统,C 版交给 goto done 清理链**。

本实践需要 CUDA GPU 与可用的 libcvcuda 环境;若当前机器没有 GPU,可先完成代码并标注「待本地验证」,同时把 4.1.4 与 4.3.4 两个不依赖 GPU 的观察实践做掉。

## 6. 本讲小结

- 每个算子都有 `.h`(C 接口)与 `.hpp`(RAII C++ 类)双份定义;C ABI 是稳定契约,C++ 类只是 C API 的零逻辑薄糖,两份文件可互相推导着读。
- C++ 侧生命周期全自动:构造调 `cvcudaXxxCreate`(失败抛 `nvcv::Exception`),`operator()` 调 `Submit`,析构经 `detail::OperatorHandle` 恰好一次 `nvcvOperatorDestroy`——拷贝删除、移动转移是防双销毁的机器保障。
- C 侧生命周期全手动:算子是 Create/Destroy 式,张量却是引用计数式(`nvcvTensorDecRef`),两种模型不要混淆;错误只经 `NVCVStatus` 返回码传递,消息用 `nvcvGetLastErrorMessage` 取。
- `src/cvcuda/OpXxx.cpp` 是两层之间的异常边界:`ProtectCall` 把 C++ 异常翻译成错误码,`CheckThrow` 反向翻译;`TensorWrapHandle` 不增引用地穿句柄,`ToDynamicRef` 用 dynamic_cast 保证传错句柄得错误码而非崩溃。
- 流语义与语言无关:算子可在同步前析构,张量的释放(或 DecRef)必须等到流同步之后。

## 7. 下一步学习建议

本讲只回答了「怎么调」。下一讲 **u6-l2 错误处理与符号版本** 沿着本讲出现的 `ProtectCall`、`CVCUDA_DEFINE_API` 两条线索深挖:Status/Exception 体系的完整对照表,以及 `CVCUDA_DEFINE_API(0, 2, ...)` 里那两个数字(0, 2)如何实现跨版本 ABI 兼容。如果你更关心「显存从哪来」,可跳读 **u6-l3 分配器**,看 `nvcvTensorConstruct` 第二个参数背后的 Allocator 抽象。建议同步动手:把综合实践的两个程序扩展成一个小工具,接受任意图片路径与 flipCode,用命令行参数驱动——它会自然引出错误处理的全部分支。

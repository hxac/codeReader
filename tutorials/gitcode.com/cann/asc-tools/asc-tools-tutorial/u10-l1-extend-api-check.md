# 扩展 API 校验器与二次开发

## 1. 本讲目标

本讲是「专家层」的第一篇，目标读者已经读过 u4-l1（校验基类）、u4-l2（DataCopy 搬运校验）、u3-l3（Stub 注册与内建函数转义），理解了 api_check 校验框架「入口函数 → 校验器子类 → 基类通用检查」的三层结构，以及它是如何被 npuchk 的 stub 钩子驱动的。

本讲不再讲「框架是什么」，而是讲「**怎么在框架上动手**」。读完本讲，你应当能够：

1. 说出**新增/增强一个 API 校验器**的完整代码骨架：在哪里写入口函数、在哪里写校验器子类、如何复用基类通用检查。
2. 能复用 `CheckTensorSizeOverflow`、`CheckTensorScope`、`CheckTensorAddrAlign` 等基类方法，**不重复造轮子**。
3. 清楚 cpudebug 的**闭源/开源边界**——哪一层你可以自由改、哪一层被构建期的闭源生成器锁死，从而判断一个二次开发需求是否在 asc-tools 开源仓内可达。

一句话定位：本讲把前几讲散落的「分层」收束成一份**可操作的二次开发手册**。

## 2. 前置知识

本讲默认你已掌握以下概念（若陌生，请先回看对应讲义）：

- **三层校验结构**（u4-l1）：入口函数 `CheckFuncXxxImpl` → 校验器子类 `TikcppXxxCheck` → 基类 `TikcppBaseCheck`，控制流靠返回值「失败即 `return false`」自底向上短路传播。
- **ASCENDC_CHECK 宏**（u4-l1）：`if (!(x)) return false;` 的短路语义，配合 `CHECK_LOG_ERROR` 同时输出到 stdout 与 dlog。
- **向量指令的访问脚印**（u4-l3）：block（32B）/ repeat（256B=8 block）/ repeatTimes / mask / stride，校验的本质是算「最远端点」再与 Tensor 容量比大小。
- **Stub 注册表**（u3-l3）：cpudebug 维护一张 `(fid, type)` 二维函数表，分 AscendC / cceprint / npuchk 三类前缀，由构建期脚本生成。

如果你是直接跳到本讲的进阶读者，至少需要记住一个心智模型：**当算子在 CPU 域被执行时，每个 Ascend C 内建函数（如 `DataCopy`、`Add`）除了真正干活，还会通过 npuchk 类别的 stub 触发对应的 `CheckFuncXxxImpl`，把本次调用的参数送进 api_check 做静态合法性校验，违例记入 `*_npuchk.log`。** 本讲授你如何在这条链上「加装」自己的检查。

## 3. 本讲源码地图

本讲围绕「写一个校验器」需要触碰的全部源码展开：

| 文件 | 作用 | 在本讲中的角色 |
| --- | --- | --- |
| `cpudebug/src/api_check/kernel_check_util.cpp` | 入口函数 `CheckFuncXxxImpl` 集中定义 | **扩展点 1**：注册新校验的入口 |
| `cpudebug/src/api_check/kernel_data_copy_check.cpp` | `TikcppDataCopyCheck` 子类实现 | **范本**：最简单的搬运类校验器 |
| `cpudebug/src/api_check/inc/kernel_data_copy_check.h` | `TikcppDataCopyCheck` 类声明 | 展示子类如何继承基类、持有 `param_` |
| `cpudebug/src/api_check/kernel_vec_binary_check.cpp` | `TikcppVecBinaryCheck` 子类实现 | **范本**：复用基类检查最完整的例子 |
| `cpudebug/src/api_check/kernel_base_check.cpp` | `TikcppBaseCheck` 基类 + 通用数学函数 | **复用对象**：通用检查都在这里 |
| `cpudebug/src/api_check/inc/kernel_base_check.h` | 基类声明、`TensorOverflowParams`、`ModeType` | 查阅通用检查的签名 |
| `cpudebug/src/api_check/kernel_check_params.h` | `ASCENDC_CHECK` 等宏、`bufferSizeMap` | 错误报告与硬件容量表 |
| `cpudebug/utils/include/utils/kernel_check_data_copy_util.h` | 搬运类参数结构体 `DataCopyApiParams` 等 | **扩展点 2**：参数如何打包 |
| `cpudebug/CMakeLists.txt` | `ASCENDC_CHECK_SRC` 的 GLOB 收集、闭源 model 库链接 | **闭源/开源边界**的物证 |
| `tests/ut/testcase/tikcpp_api_check/test_data_copy_check.cpp` | 校验器单元测试 | 给新校验写测试的范本 |

定位口诀：**写校验看 `kernel_*_check.cpp`、查通用武器看 `kernel_base_check.*`、接线入口看 `kernel_check_util.cpp`、改构建看 `cpudebug/CMakeLists.txt`。**

## 4. 核心概念与源码讲解

### 4.1 校验器扩展点

#### 4.1.1 概念说明

「扩展点」回答的是：**我想加一条新检查，代码应该落在哪一层、按什么顺序写。**

回顾 u4-l1 的三层结构，每一层都是一个扩展点，但「改动力度」从上到下递增：

| 扩展点 | 文件 | 改动方式 | 难度 |
| --- | --- | --- | --- |
| A. 增强已有校验器 | `kernel_xxx_check.cpp` | 在现有 `TikcppXxxCheck` 里加一个私有方法，挂到 `CheckAllHighLevel()` | 最易，推荐 |
| B. 新增校验器子类 | 新增 `kernel_yyy_check.cpp/.h` | 继承 `TikcppBaseCheck`，实现 `CheckAllHighLevel()` | 中等 |
| C. 新增入口函数 | `kernel_check_util.cpp` | 加一个 `CheckFuncYyyImpl` 把子类串起来 | 中等 |

A、B、C 三层的代码**全部开源、全部在 `src/api_check/` 目录下**，且会被 CMake 自动收集（见 4.3）。这意味着：**从「写一个校验器子类」到「它能被编译进 `libcpudebug.so`」之间没有任何手动接线**——这是 asc-tools 留给二次开发者最友好的口子。

#### 4.1.2 核心流程

新增一个搬运类 API 校验器，按「自下而上」顺序完成三步：

```text
第 1 步（数据层）：在 utils/include/utils/kernel_check_yyy_util.h 里定义参数结构体 YyyApiParams
                  ——把 host 侧收集到的地址/dtype/scope/stride 等打包成一个 struct
第 2 步（子类层）：新增 kernel_yyy_check.h/.cpp
                  ——class TikcppYyyCheck : public TikcppBaseCheck
                  ——构造函数把 apiName 与 YyyApiParams& 传给基类与成员
                  ——实现 CheckAllHighLevel()，内部用 ASCENDC_CHECK(...) 串联若干检查
第 3 步（入口层）：在 kernel_check_util.cpp 里新增 CheckFuncYyyImpl
                  ——ASCENDC_CHECK_INTRI_NAME 校验名称非空
                  ——构造 TikcppYyyCheck chkIns{intriName, chkParams}
                  ——调用 chkIns.CheckAllHighLevel()，返回值原样上抛
```

这三步的「模板代码」非常固定，下面逐一对照真实源码。

#### 4.1.3 源码精读

**入口层的标准模板**（最短范本——`CheckFuncDataCopyImpl`）：

[cpudebug/src/api_check/kernel_check_util.cpp:61-69](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp#L61-L69) 定义了 DataCopy 的入口函数。它只做三件事：① `ASCENDC_CHECK_INTRI_NAME` 校验内建函数名非空；② 构造子类实例 `chkIns`；③ 调用 `CheckAllHighLevel()` 并把布尔结果返回。

其中名称校验宏 [cpudebug/src/api_check/kernel_check_util.cpp:23-29](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp#L23-L29) 是所有入口函数共用的「哨兵」，名字为空就直接 `return false`，避免后续日志里出现空 API 名。

> 关键点：**入口函数本身不做任何业务检查**，它只是「构造子类 + 调一个方法」。所有逻辑都下沉到子类。这是你抄模板时要遵守的纪律——入口保持薄。

**子类层的标准模板**（DataCopy 校验器）：

[cpudebug/src/api_check/inc/kernel_data_copy_check.h:22-32](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_data_copy_check.h#L22-L32) 声明 `TikcppDataCopyCheck`。注意三处细节：① `public TikcppBaseCheck` 继承基类，从而白嫖全部通用检查；② 构造函数 `TikcppDataCopyCheck(const std::string& name, DataCopyApiParams& param)` 把 `name` 转交基类（基类用它填充 `apiName`，所有错误信息都会带上它）、把 `param` 存为成员引用 `param_`；③ `~TikcppDataCopyCheck() override = default;` 显式标注 override。

[cpudebug/src/api_check/kernel_data_copy_check.cpp:21-25](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_check.cpp#L21-L25) 是 `CheckAllHighLevel()` 的实现——目前只调用 `CheckAddrAlign()`，外面套一层 `ASCENDC_CHECK(...)`。这就是「扩展点 A」最典型的落点：**你想给 DataCopy 加新检查，就在这个函数里再加一行 `ASCENDC_CHECK(CheckSomething());`。**

**参数结构体模板**：

[cpudebug/utils/include/utils/kernel_check_data_copy_util.h:115-124](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_check_data_copy_util.h#L115-L124) 定义 `DataCopyApiParams`。它继承自 `DataCopyBaseParams`（[L81-113](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_check_data_copy_util.h#L81-L113)），后者在构造函数里做了一件重要的事：把外部传入的「逻辑位置 `dstPosIn`」经 `GetPhyType(static_cast<TPosition>(...))` 转成「物理位置 `dstPos`」（[L93-L94](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/utils/include/utils/kernel_check_data_copy_util.h#L93-L94)）。所以每个参数结构体都同时携带 `dstLogicPos`/`srcLogicPos`（给 scope 检查用）与 `dstPos`/`srcPos`（给对齐/容量检查用）。新增结构体时务必沿用这个「双份位置」约定。

#### 4.1.4 代码实践

**实践目标**：在不碰闭源代码的前提下，给 `TikcppDataCopyCheck` 增加一条「源/目的 dtype 字节数必须一致」的检查，并验证它能被编译进库。

**操作步骤**（源码阅读型 + 局部修改型，注意只能在你自己的 fork 里改，不要污染原仓库）：

1. 打开 [cpudebug/src/api_check/inc/kernel_data_copy_check.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_data_copy_check.h)，在 `private:` 区新增一个方法声明：`bool CheckDtypeConsistent();`
2. 打开 [cpudebug/src/api_check/kernel_data_copy_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_check.cpp)，实现该方法：当 `param_.srcDtypeBytes != param_.dstDtypeBytes` 时，用 `CHECK_LOG_ERROR(...)` 报错并 `return false;`（参考 `kernel_data_copy_pad_check.cpp` 的 `CheckPadParamters` 写法，见 [kernel_data_copy_pad_check.cpp:21-48](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_pad_check.cpp#L21-L48)）。
3. 在 `CheckAllHighLevel()`（[kernel_data_copy_check.cpp:21-25](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_check.cpp#L21-L25)）里加一行 `ASCENDC_CHECK(CheckDtypeConsistent());`。
4. 重新执行 `bash build.sh --test`（详见 u9-l3），CMake 的 GLOB 会自动把改动重新编译进各架构的 `libcpudebug.so`，无需改任何构建脚本。

**需要观察的现象**：编译期 `ASCENDC_CHECK_SRC` 重新收集了 `kernel_data_copy_check.cpp`，无链接错误。

**预期结果**：`build_out/` 下各产品的 `libcpudebug.so` 重新生成；此后任何 `DataCopy` 调用若源/目的 dtype 不一致，运行期会在 `*_npuchk.log` 里打出一条 `[ERROR] ... in DataCopy ...`。**待本地验证**（本环境无 CANN 包，无法实跑）。

#### 4.1.5 小练习与答案

**练习 1**：入口函数 `CheckFuncDataCopyImpl` 里为什么不直接写对齐检查，而要转交给子类？

> **参考答案**：入口层只负责「接线」（名称校验 + 构造子类 + 调用），业务逻辑全部下沉子类。这样同一个子类可以被多个入口（high-level / low-level / maskArray 三个重载，见 `CheckFuncVecBinaryImpl` 的三个版本 [kernel_check_util.cpp:101-120](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_util.cpp#L101-L120)）复用，也方便单独对子类做单元测试。

**练习 2**：如果你想给 DataCopy 既加「high-level」检查又加「带 mask 的 low-level」检查，需要在类里实现哪两个方法、分别在哪个入口被调用？

> **参考答案**：实现 `CheckAllHighLevel()` 与 `CheckAllLowLevel(std::vector<uint64_t> maskArray)`。前者被 `CheckFuncDataCopyImpl(..., intriName)` 调用，后者被带 mask 的重载（类似 `CheckFuncVecBinaryImpl(..., mask, intriName)`）调用。

### 4.2 通用 util 复用

#### 4.2.1 概念说明

「通用 util 复用」回答的是：**写新检查时，哪些轮子已经造好、直接调用即可，不要自己写。**

基类 `TikcppBaseCheck` 与一批自由函数把「与具体 API 无关的检查」全部抽到了公共层。你在子类里要做的事，本质上是「把本次调用的参数，翻译成这些公共函数的入参」。复用它们不仅省代码，更重要的是保证**不同 API 的同一类约束（如越界、对齐、scope）行为一致、错误信息格式统一**。

公共武器库分两抽屉：

- **基类成员方法**（带 `apiName`，错误信息自带 API 名）：scope / 容量 / mask / 对齐 / 越界检查。
- **命名空间自由函数**（需手动传 `apiName`）：`CheckTensorSizeOverflow`、`GetMaskLength`、`CalculateVectorMaxOffset`、`CounterSplitMainTail`。

#### 4.2.2 核心流程

写一条新检查的「翻译」流程：

```text
1. 想清楚这条检查属于哪一类约束：
   scope（存储位置） / 容量（不超硬件上限） / 越界（访问脚印不超分配）
   / 对齐（起始地址对齐） / mask 合法性 / 参数取值范围
2. 在基类里找对应方法：
   scope     → CheckTensorScope(logicPos, expectedPos, tensorInfo, posInfo)
   容量      → CheckBufferSizeOverFlow(localSize, bufferSize, errMsg)
   越界(high)→ CheckTensorOverflowHigh(dtypeSize, bufferSize, calCount, tensorName)
   越界(low) → CheckTensorOverflowLow(maskArray, TensorOverflowParams, tensorName)
   对齐      → CheckTensorAddrAlign(tensorAddr, phyPos, alignBytes, tensorInfo)
   mask      → UpdateMaskArrayAndCheck(maskArray, maxByteLen) / CheckMaskArray / CheckMaskImm
3. 用 ASCENDC_CHECK(基类方法(...)) 包起来，失败即短路
```

其中「越界」是本讲实践任务要复用的核心，它的数学模型（来自 u4-l1/u4-l3）一句话概括：

\[ \text{maxOffset} = \big((\text{repeatTimes}-1)\cdot\text{repStride} + (\text{blkNumLastRep}-1)\cdot\text{blkStride}\big)\cdot\text{blockLen} + \text{eleNumLastBlk} \]

再乘以 `dtypeBytes` 得到字节数，与 `tensorSize` 比较。

#### 4.2.3 源码精读

**复用范本之首——`CheckTensorSizeOverflow`**（自由函数，几乎所有越界检查的最底层）：

[cpudebug/src/api_check/kernel_base_check.cpp:53-70](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L53-L70) 实现：比较 `expectedSize <= tensorSize`，失败时经 `ASCENDC_CHECK_AND_LOG` 打印「至少需要 X 字节，当前只有 Y 字节」并 `return false`；`ModeType` 仅在错误后缀里加 ` when in normal mode` / ` when in counter mode`，**不改变比较逻辑**。注意它接收 `apiName` 参数——这就是为什么子类构造时必须把名字传给基类。

`CalculateVectorMaxOffset`（[kernel_base_check.cpp:228-240](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L228-L240)）就是上面公式的代码化：先算最后一个 repeat 需要几个 block、尾 block 有几个有效元素，再套几何式子。`GetMaskLength`（[L25-51](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L25-L51)）扫描 mask 的最高置位 bit 得到「每个 repeat 算几个元素」，并对 `dtypeSize >= 4` 字节的类型封顶到 `DEFAULT_BLOCK_SIZE/dtypeSize`。

**基类成员方法清单**（全部带 `apiName`，子类直接 `this->` 调用）：

[cpudebug/src/api_check/inc/kernel_base_check.h:78-175](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h#L78-L175) 声明 `TikcppBaseCheck`。最常用的几个：

- [CheckTensorScope L92-94](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h#L92-L94)：校验逻辑位置经 `GetPhyType` 转成物理位置后，是否等于期望位置（如向量运算要求三个 tensor 都在 UB）。
- [CheckBufferSizeOverFlow L101](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h#L101)：比较分配大小与硬件容量上限（容量来自 `bufferSizeMap`）。
- [CheckTensorOverflowHigh L146-148](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h#L146-L148)：high-level（按元素计数）越界检查，内部转调 `CheckTensorSizeOverflow`（见 [kernel_base_check.cpp:72-82](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L72-L82)）。
- [CheckTensorOverflowLow L122-123](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h#L122-L123)：low-level（带 repeat/mask/stride）越界检查，自动按 counter/normal 模式分流。
- [CheckTensorAddrAlign L169-171](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h#L169-L171)：起始地址对齐检查，对齐粒度由调用方按物理位置决定。
- [UpdateMaskArrayAndCheck L158](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h#L158)：若用户未显式设 mask，用寄存器里的 `maskHigh/maskLow` 替换，并校验。

**复用集大成者——`TikcppVecBinaryCheck`**（几乎用遍了基类武器）：

[cpudebug/src/api_check/kernel_vec_binary_check.cpp:49-67](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_binary_check.cpp#L49-L67) 的 `CommonCheck()` 是写新子类时最好的抄写对象——它用 `CheckTensorScope` 把 dst/src0/src1 都钉在 UB，用 `CheckBufferSizeOverFlow` 配 `bufferSizeMap.at(pos)` 查容量，最后调自身的 `CheckAddrAlign()`。注意 [L56-64](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_binary_check.cpp#L56-L64) 取容量的写法：`GlobalParams::Instance().bufferSizeMap.at(param_.dstPos)`——容量表定义在 [kernel_check_params.h:94-100](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_check_params.h#L94-L100)，按物理位置 `HardWareIndex` 索引 `UB_SIZE`/`L1_SIZE` 等。

[L108-120](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_vec_binary_check.cpp#L108-L120) 的 `CheckAllHighLevel()` 则示范了 high-level 越界检查：直接复用基类 `CheckTensorOverflowHigh`，并对 `Compare` 类 API 走专用变体 `CheckCmpTensorOverflowHigh`。这种「公共检查 + 个别 API 特例」的写法，正是扩展点 A 的精髓。

#### 4.2.4 代码实践

**实践目标**：手工走一遍「翻译」——给 DataCopy 的高层校验补一条越界检查，复用 `CheckTensorSizeOverflow`，并明确它会被哪一层调用。

**操作步骤**：

1. 在 `TikcppDataCopyCheck` 里新增私有方法 `bool CheckCopyOverflow();`。
2. 实现里，根据搬运的字节数计算 `expectedSize`。最简形式（按 blockCount × blockLen 估算）：

   ```cpp
   // 示例代码（非项目原有，仅演示骨架）
   bool TikcppDataCopyCheck::CheckCopyOverflow()
   {
       // DataCopyBaseParams 里 blockCount/blockLen 单位是 block(32B)/元素，这里按字节估算下界
       uint64_t expectedSize = static_cast<uint64_t>(param_.blockCount) *
                               static_cast<uint64_t>(param_.blockLen) *
                               param_.srcDtypeBytes;
       // 复用基类同款比较：expectedSize <= srcSize / dstSize
       ASCENDC_CHECK(CheckTensorSizeOverflow(expectedSize, param_.srcSize, "src", apiName));
       ASCENDC_CHECK(CheckTensorSizeOverflow(expectedSize, param_.dstSize, "dst", apiName));
       return true;
   }
   ```

3. 在 `CheckAllHighLevel()`（[kernel_data_copy_check.cpp:21-25](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_check.cpp#L21-L25)）里加 `ASCENDC_CHECK(CheckCopyOverflow());`。

**需要观察的现象**：编译通过；运行一个 `DataCopy` 越界用例时，`*_npuchk.log` 出现 `Failed to check src size in DataCopy, tensor size needs to be at least N bytes ...`。

**预期结果**：错误信息里的 `DataCopy` 来自基类 `apiName` 成员（构造时由入口函数传入），`at least N bytes` 来自 [CheckTensorSizeOverflow 的格式串](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L63-L68)。

**「会被哪一层调用」的答案**（实践任务的关键问）：

```text
CPU 域算子调用 DataCopy(...)
   └─ npuchk 类别 stub（构建期生成，闭源接线）
        └─ CheckFuncDataCopyImpl          ← 入口层（kernel_check_util.cpp:61，开源）
             └─ TikcppDataCopyCheck::CheckAllHighLevel()   ← 子类层（开源，你改的就是这里）
                  ├─ CheckAddrAlign → 基类 CheckTensorAddrAlign
                  └─ CheckCopyOverflow → 自由函数 CheckTensorSizeOverflow  ← 基类层（开源）
```

也就是说：**你新写的 `CheckCopyOverflow` 由子类层调用，它复用的 `CheckTensorSizeOverflow` 属于基类层（自由函数），最终被入口层 `CheckFuncDataCopyImpl` 驱动。** 整条开源链路里，唯一不归你管的是最顶端「stub → 入口函数」的接线（见 4.3）。**待本地验证。**

#### 4.2.5 小练习与答案

**练习 1**：`CheckTensorSizeOverflow` 是自由函数，需要显式传 `apiName`；而 `CheckTensorOverflowHigh` 是基类成员，不用传。为什么后者不用传？

> **参考答案**：`CheckTensorOverflowHigh` 是 `TikcppBaseCheck` 的成员方法，能直接读到基类 `apiName` 成员（[kernel_base_check.h:174](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_base_check.h#L174)），并在内部转调 `CheckTensorSizeOverflow(..., apiName)`（[kernel_base_check.cpp:80](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L80)）。自由函数没有 `this`，所以必须显式传名。

**练习 2**：写一条新检查时，如何决定用 `CheckTensorOverflowHigh` 还是 `CheckTensorOverflowLow`？

> **参考答案**：看 API 暴露的参数模型。high-level API（如 high-level `Add(dst, src, count)`）只给元素计数 `calCount`，用 `CheckTensorOverflowHigh`；low-level API（带 `repeatTimes`/`mask`/`blkStride`/`repStride`）需按访问脚印算最远端点，用 `CheckTensorOverflowLow` 配 `TensorOverflowParams`。`DataCopy` 这类搬运 API 既不是向量 repeat 模型，可直接用最底层的 `CheckTensorSizeOverflow` 自行估算字节数。

### 4.3 闭源/开源边界

#### 4.3.1 概念说明

「闭源/开源边界」回答的是：**我的二次开发需求，哪些在 asc-tools 开源仓内能闭环，哪些会被卡住。**

cpudebug 是一个「**开源校验代码 + 闭源模型库**」的混合体。理解边界，才能判断一个改动的可行性，避免在闭源的墙上撞头。先给结论：

- **全开源、可自由改**：整个 `src/api_check/` 目录（入口函数 + 校验器子类 + 基类 + 数学函数）、`utils/include/utils/kernel_check_*_util.h`（参数结构体）、`src/regfwk/` 的部分注册/回溯代码。
- **闭源、不可改**：`libraries/lib/<product>/libcpudebug_model.a`（NPU 仿真模型，被 `ar -x` 拆成 `.o` 链接进来）；构建期生成 stub 注册项的脚本（`write_npuchk.py` 等）及其产物。
- **被闭源锁住的环节**：让一个 Ascend C 内建函数调用「路由到」某个 `CheckFuncXxxImpl` 的接线——它由构建期脚本按 `(fid, type)` 生成 npuchk 类别 stub（回顾 u3-l3 的 5149×5 函数表）。

#### 4.3.2 核心流程

把一个「我想给某 API 加校验」的需求，按边界拆成两类：

```text
需求类型 A：给「已有 stub 接线」的 API 增强校验（如 DataCopy/Add/Reduce…）
  → 完全在开源仓内闭环
  → 改对应 kernel_xxx_check.cpp 的 CheckAllHighLevel() 即可
  → CMake GLOB 自动重新编译进 libcpudebug.so

需求类型 B：给「尚无 stub 接线」的新增/私有内建函数挂校验
  → 受闭源生成器阻挡
  → 你能写出 CheckFuncYyyImpl + TikcppYyyCheck，但
    「让该函数在新内建函数被调用时触发」需要 npuchk 类别 stub，
    而该 stub 由闭源 write_npuchk.py 在构建期生成
  → 开源仓内无法让接线自动生效；只能靠单元测试直接调 CheckFuncYyyImpl 验证逻辑
```

换句话说：**扩展点 A/B/C（写子类、写入口）永远开放；而「把入口接进调用链」的最顶端那一跳，受闭源生成器约束。** 这就是为什么本讲的实践都聚焦在「改子类 + 单元测试验证」——这是开源仓内最完整可达的闭环。

#### 4.3.3 源码精读

**物证 1：开源校验代码如何被自动收集**

[cpudebug/CMakeLists.txt:44-46](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L44-L46) 用 `file(GLOB ... src/api_check/*.cpp)` 把整个 api_check 目录的 `.cpp` 全部收进 `ASCENDC_CHECK_SRC`。这意味着：**你新增的 `kernel_yyy_check.cpp` 会被自动编译，无需修改 CMakeLists。** 这是扩展点 B（新增子类）能成立的基石。

**物证 2：闭源模型库如何被并入**

[cpudebug/CMakeLists.txt:54-66](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L54-L66) 是关键的「混合」循环：对每个 `product_type`，先 `execute_process(COMMAND ${CMAKE_AR} -x libcpudebug_model.a ...)` 把闭源静态库拆成 `.o`（[L55-59](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L55-L59)），再用 `IMPORTED_OBJECTS` 把它们与开源的 `ASCENDC_CHECK_SRC` + `ASCENDC_REGFWK_SRC` 一起链接成 `cpudebug_<product>` 共享库（[L68-72](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L68-L72)）。`libcpudebug_model.a` 存放在 `libraries/lib/<product>/`——它不在源码树里以 `.cpp` 形式存在，你只能拿到编译好的目标文件。

**物证 3：哪些产物来自闭源生成器**

[cpudebug/CMakeLists.txt:245-252](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L245-L252) 直接安装三个预编译的 `.so`：`libcpudebug_cceprint.so`、`libcpudebug_npuchk.so`、`libcpudebug_stubreg.so`。回顾 u3-l3，这三个库分别对应 cceprint/npuchk/AscendC 三类 stub 的**生成产物**——它们由构建期脚本（`cce_stub.py`/`write_npuchk.py`/`reg_funs_gen.py`）生成，以二进制 `.so` 形式随仓分发。其中 `libcpudebug_npuchk.so` 内含「内建函数符号 → `CheckFuncXxxImpl`」的 npuchk 类别 stub 接线，正是需求类型 B 触不到的那一跳。

> 梳理三条边界线：
> - 你能改：`src/api_check/*.cpp`（校验逻辑）、`utils/include/utils/kernel_check_*_util.h`（参数结构体）。
> - 你能编进但不能改源：`libraries/lib/<product>/libcpudebug_model.a`（模型仿真）。
> - 你既不能改源、也不能重新生成（脚本不随仓分发）：`libcpudebug_npuchk.so` 等 stub 接线产物。

#### 4.3.4 代码实践

**实践目标**：通过单元测试，在「不依赖闭源 stub 接线」的前提下，验证你新写的校验逻辑正确。

**操作步骤**（源码阅读型，对照真实测试）：

1. 打开 [tests/ut/testcase/tikcpp_api_check/test_data_copy_check.cpp:229-240](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/testcase/tikcpp_api_check/test_data_copy_check.cpp#L229-L240)。注意它的写法：构造 `DataCopyApiParams chkParams{...}`，然后**直接调用** `CheckFuncDataCopyImpl(chkParams, "DataCopy")`，用 `EXPECT_EQ(flag, param.expect)` 断言。这就是绕过 stub、直测入口函数的范式。
2. 阅读 [tests/ut/testcase/tikcpp_api_check/api_check_test_utils.h:26-31](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/testcase/tikcpp_api_check/api_check_test_utils.h#L26-L31) 的 `MakeTensor(pos, byteSize)`：它用 `ConstDefiner::GetHardwareBaseAddr(hardPos)` 取真实硬件基地址，返回 `{addr, length, pos}`，`LogicPos(tensor)`（[L33](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/testcase/tikcpp_api_check/api_check_test_utils.h#L33)）取出逻辑位置。你的新测试可以用它构造 tensor。
3. 仿照 `TEST_P(TestDataCopyApiCheckSuite, ...)`（[L229](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/testcase/tikcpp_api_check/test_data_copy_check.cpp#L229)）的参数化结构，为你的新检查加一组 `{输入参数, 期望结果}` 用例，`expect=false` 的用例用来验证越界/违例被正确捕获。

**需要观察的现象**：`bash build.sh --cpp_utest` 跑通后，你的新用例 PASS（合法用例返回 true、违例用例返回 false）。

**预期结果**：即便没有 NPU、没有 stub 接线，单元测试也能证明你的校验逻辑正确——这正是开源仓内对需求类型 A 最完整的验证闭环。**待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：同事想在 asc-tools 里给一个自研的私有内建函数 `MyOp` 加 CPU 域校验。他写了 `TikcppMyOpCheck` 和 `CheckFuncMyOpImpl`，但发现实际跑算子时检查没被触发。结合本节，最可能的原因是什么？

> **参考答案**：触发的「最后一跳」——让 `MyOp` 的 npuchk 类别 stub 调用 `CheckFuncMyOpImpl`——是由闭源构建期脚本 `write_npuchk.py` 生成、编译进 `libcpudebug_npuchk.so` 的。开源仓不提供该脚本，所以他写好了开源侧的子类与入口，却无法生成对应的 stub 接线，导致调用链断在最顶端。可行兜底是先用单元测试直调 `CheckFuncMyOpImpl` 验证逻辑，并向 CANN 团队提需求把接线纳入构建。

**练习 2**：为什么新增一个 `kernel_yyy_check.cpp` 不需要改 `cpudebug/CMakeLists.txt`？

> **参考答案**：因为 [CMakeLists.txt:44-46](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/CMakeLists.txt#L44-L46) 用的是 `file(GLOB src/api_check/*.cpp)`，CMake 配置期会自动把该目录下所有 `.cpp`（含新增文件）收进 `ASCENDC_CHECK_SRC`，并参与每个 `cpudebug_<product>` 目标的编译。新增文件只要落在 `src/api_check/` 即被自动纳入。

## 5. 综合实践

把本讲三个最小模块串起来，完成一个迷你需求：**为 DataCopy 增加一条「搬运字节数不得超出源/目的 Tensor 分配大小」的越界校验，并给它配一条单元测试。**

完整步骤：

1. **定位扩展点**（4.1）：改动落在子类层 `TikcppDataCopyCheck`（扩展点 A），不动入口层与基类。
2. **复用通用 util**（4.2）：越界检查复用自由函数 `CheckTensorSizeOverflow`（[kernel_base_check.cpp:53-70](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_base_check.cpp#L53-L70)），不要自己写比较与日志。
3. **编写骨架**：在 [kernel_data_copy_check.h](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/inc/kernel_data_copy_check.h) 加 `bool CheckCopyOverflow();` 声明；在 [kernel_data_copy_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_check.cpp) 实现（参考 4.2.4 的示例代码），并在 `CheckAllHighLevel()` 里挂上 `ASCENDC_CHECK(CheckCopyOverflow());`。
4. **闭源边界**（4.3）：DataCopy 已有 stub 接线（属需求类型 A），所以改子类即可让校验在真实调用时生效，无需碰闭源生成器。
5. **单元测试**：在 [test_data_copy_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/tests/ut/testcase/tikcpp_api_check/test_data_copy_check.cpp) 仿照 `TestDataCopyApiCheckSuite` 加一组参数化用例——构造一个 `blockCount*blockLen*dtypeBytes > srcSize` 的越界场景，`expect=false`，用 `MakeTensor` 取地址，断言 `CheckFuncDataCopyImpl` 返回 false。
6. **验证**：`bash build.sh --cpp_utest` 跑测试；`bash build.sh --pkg` 出 run 包后，在 add 样例里故意把 DataCopy 的 `blockLen` 调大到越界，观察 `*_npuchk.log` 是否打出 `Failed to check src size in DataCopy ...`。

**自检问题**：你的新检查最终是被哪一层调用的？错误信息里的 `DataCopy` 字样从哪里来？

> **参考答案**：被**子类层**（`TikcppDataCopyCheck::CheckAllHighLevel`）调用，复用的 `CheckTensorSizeOverflow` 属于**基类层**自由函数，整条链由**入口层** `CheckFuncDataCopyImpl` 驱动。`DataCopy` 字样来自基类 `apiName` 成员——它由入口函数构造子类时传入（`chkIns{intriName, chkParams}` 的第一个参数），再被 `CheckTensorSizeOverflow` 的 `apiName` 形参透传到日志格式串。

## 6. 本讲小结

- **三个扩展点，自下而上**：参数结构体（util 头）→ 校验器子类（`kernel_*_check.cpp`，继承 `TikcppBaseCheck`）→ 入口函数（`kernel_check_util.cpp` 的 `CheckFuncXxxImpl`）。前两层是日常改动的主战场。
- **入口函数有固定模板**：`ASCENDC_CHECK_INTRI_NAME` → 构造子类 → 调 `CheckAllHighLevel()`/`CheckAllLowLevel()`，入口本身不做业务检查。
- **通用武器集中在基类**：`CheckTensorScope`/`CheckBufferSizeOverFlow`/`CheckTensorOverflowHigh`/`CheckTensorOverflowLow`/`CheckTensorAddrAlign` 是成员方法（自带 `apiName`）；`CheckTensorSizeOverflow`/`GetMaskLength`/`CalculateVectorMaxOffset` 是自由函数（需传 `apiName`）。写新检查 = 把参数翻译成这些函数的入参。
- **开源/闭源边界清晰**：`src/api_check/*.cpp` 全开源且被 CMake GLOB 自动收集；`libcpudebug_model.a`（仿真模型）以目标文件形式闭源并入；`libcpudebug_npuchk.so` 等 stub 接线产物由不随仓分发的脚本生成。
- **二次开发的现实约束**：增强已有 API 校验（需求 A）在开源仓内完全闭环；给尚无 stub 接线的新内建函数挂校验（需求 B）会被闭源生成器卡在「最后一跳」，可用单元测试直调入口函数验证逻辑。
- **验证闭环**：用 gtest 参数化用例（`MakeTensor` 取地址 + 直调 `CheckFuncXxxImpl`）可在无 NPU 环境下证明校验逻辑正确。

## 7. 下一步学习建议

- **接着读 u10-l2（贡献流程与代码规范）**：本讲的局部改动如何通过 pre-commit、OAT 合规检查进入正式贡献；`scripts/` 下的 `run_presmoke.sh` 如何在提交前跑通编译与 UT。
- **横向对照 u9-l3（单元测试体系）**：本讲只示范了 `--cpp_utest` 的 api_check 测试；若要看 `--asan`、覆盖率与 Python UT 的全貌，回看 u9-l3。
- **深挖闭源边界**：若你的需求触及需求类型 B，建议顺带阅读 u3-l3 的 `StubReg`/`g_regStubs` 与 `write_npuchk.py` 的关系，理解「为什么最后一跳不可开源侧达成」，并据此撰写对 CANN 团队的需求。
- **扩展阅读**：挑一个尚未复用全部武器的校验器（如 [kernel_data_copy_slice_check.cpp](https://github.com/gitcode.com/cann/asc-tools/blob/c6f35b0cf219c53f3c485fc7cef8581c53c11772/cpudebug/src/api_check/kernel_data_copy_slice_check.cpp)），尝试为它补一条复用 `CheckTensorOverflowLow` 的 low-level 越界检查，作为本讲的综合训练延伸。

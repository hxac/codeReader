# u6-l3 ONNX/TF 框架插件适配层

> 本讲为增量更新版本。上一 HEAD（2bd9cb7c）到当前 HEAD（394ba763）之间，本讲涉及的源码仅有一处变化：`common/src/framework/psroi_poolingV2_onnx_plugin.cpp` 中一条 `OP_LOGE` 日志文案由中文顿号分隔改为英文逗号分隔（行号未变，见 4.4.3）。其余插件源码与编译脚本无变化，本讲按当前源码全新撰写。

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「框架插件（framework plugin）」在 ONNX/TF 模型迁移到昇腾 NPU 这条链路上的位置和作用。
2. 掌握 `npu_xxx_onnx_plugin.cpp` 这类插件文件的标准实现套路：`REGISTER_CUSTOM_OP` 注册 + `ParseParamsXxx` 属性翻译。
3. 理解 `common/src/framework` 与算子内 `framework` 目录两级插件源的组织方式，以及它们如何被 CMake 收集、编译成 `liboponnx_plugin_*.so` 动态库。
4. 能独立读懂一个插件，并写出它的「注册接口—属性解析—下游算子」映射说明。

## 2. 前置知识

在阅读本讲之前，你需要先建立以下几个概念（前几讲已铺垫，这里快速回顾并补充新术语）：

- **模型迁移链路**：用户手里往往是一个 ONNX 或 TensorFlow 模型，而不是手写的 aclnn 调用代码。要让模型跑在 NPU 上，需要经过「模型解析 → 算子映射 → 图优化 → 编译执行」几个阶段。
- **GE（Graph Engine，图引擎）**：昇腾的图编译执行框架。模型进来后，每个外部框架的算子节点都要被翻译成一个 GE 的 `ge::Operator`，GE 才能认识并进一步调度它（回顾 u2-l4）。
- **domi**：CANN 中负责「模型转换/解析」的组件命名空间。所有插件代码都写在 `namespace domi` 里，因为插件的本质就是**模型解析器（parser）的一部分**。
- **OriginOpType 与 opset**：ONNX 模型里每个节点带一个类型字符串，如 `ai.onnx::11::RoiAlign`，其中 `11` 是 opset 版本。同一个算子在不同 opset 下语义可能微调，所以插件常按 opset 区间分别注册。
- **`ImplyType::TVM`**：注册项末尾的这个枚举告诉 GE「该算子由算子编译框架（TBE 一系）负责生成设备代码」，也就是走正常的算子编译链路，而不是插件自己执行计算。**插件只做翻译，不做计算**——这是本讲最重要的一句话。
- **protobuf Message**：ONNX 的模型文件本身就是 protobuf 序列化格式，插件拿到的 `op_src` 就是 ONNX 节点的 `NodeProto` 消息对象。

如果你对「GE 算子原型注册（REG_OP）」还不熟悉，建议先复习 u6-l1（op_graph：算子原型定义与图模式识别）。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
| --- | --- |
| `common/src/framework/` | 公共插件源目录，存放约 25 个 `*_onnx_plugin.cpp`（iou/nms/yolo/psroi_pooling/upsample 等）和 1 个 TF 桩文件 |
| `common/src/framework/npu_iou_onnx_plugin.cpp` | 最简单的 ONNX 插件样本：属性翻译 + 注册，本讲精读对象 |
| `common/src/framework/psroi_poolingV2_onnx_plugin.cpp` | 展示 JSON 字符串方式解析属性的插件，本轮有小幅日志文案修改 |
| `common/src/framework/CMakeLists.txt` | 两行脚本：收集并编译本目录下的 onnx/tf 插件源 |
| `common/inc/framework/onnx_common.h` | 插件公共工具头：取算子名、向量转 Tensor、标量 Const、格式改写 |
| `objdetect/roi_align/framework/` | 算子内 framework 目录样本，含 onnx 插件 ×2、tf 插件 ×1 |
| `objdetect/roi_align/framework/roi_align_onnx_plugin.cpp` | 进阶样本：`ParseOpToGraphFn` 子图改写 + 按 opset 双注册 |
| `objdetect/roi_align/framework/roi_align_tf_plugin.cpp` | TF 插件样本：`FusionParseParamsFn` 融合场景属性补全 |
| `cmake/func.cmake`、`cmake/variables.cmake`、`cmake/symbol.cmake` | 插件目标的创建、命名与链接安装规则 |

## 4. 核心概念与源码讲解

### 4.1 框架插件是什么：从 ONNX 节点到 GE 算子

#### 4.1.1 概念说明

假设一个检测模型里有一个 ONNX 的 `NPUIou` 节点。GE 在解析模型时不认识它——GE 只认识注册过的算子名和属性集。框架插件就是补上这一环的适配器：

```
ONNX 模型文件
   │  (protobuf 反序列化)
   ▼
NodeProto（ai.onnx::11::NPUIou，携带 ONNX 属性）
   │  (domi 插件 ParseParamsFn 回调)
   ▼
ge::Operator（GE 算子 "Iou"，携带 GE 属性 mode/eps/aligned）
   │  (ImplyType::TVM → 走算子编译链路)
   ▼
NPU 上执行
```

插件解决三个问题：

1. **名字映射**：ONNX 类型字符串 → GE 算子名（`"ai.onnx::11::NPUIou"` → `"Iou"`）。
2. **属性翻译**：ONNX 的 `AttributeProto`（带类型的键值对）→ GE 的 `SetAttr`（算子属性）。
3. **（可选）子图改写**：一个 ONNX 节点展开成多个 GE 算子组成的小子图（见 4.4）。

#### 4.1.2 核心流程

一个插件文件从加载到生效的完整流程：

1. 动态库 `liboponnx_plugin_*.so` 被模型转换工具（如 ATC）加载。
2. 文件全局作用域中的 `REGISTER_CUSTOM_OP(...)` 链式表达式在**库加载时**执行，向 GE 的注册表登记一条映射。
3. 模型解析遇到某节点时，GE 用节点的 `op_type` 字符串去注册表里匹配 `OriginOpType` 列表。
4. 命中后回调插件注册的解析函数，把 `NodeProto` 翻译成 `ge::Operator`。
5. 翻译失败返回 `FAILED`，模型转换报错终止；成功则该节点进入后续图编译流程。

#### 4.1.3 源码精读

先看全仓库最短小精悍的插件——`npu_iou_onnx_plugin.cpp`：

注册接口的六件套（后五件都是链式调用）：

```cpp
REGISTER_CUSTOM_OP("Iou")                    // ① GE 侧算子名
    .FrameworkType(ONNX)                     // ② 适配的框架
    .OriginOpType({...})                     // ③ 匹配的 ONNX 类型串（多 opset）
    .ParseParamsFn(ParseParamsNpuIou)        // ④ 解析回调
    .ImplyType(ImplyType::TVM);              // ⑤ 执行载体：交给算子编译框架
```

属性翻译函数是插件的核心逻辑，见 [common/src/framework/npu_iou_onnx_plugin.cpp:L16-L33](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/framework/npu_iou_onnx_plugin.cpp#L16-L33)：先把 `op_src` 动态转换成 ONNX 的 `NodeProto`（转换失败说明上游传错了对象，记日志返回 `FAILED`）；然后遍历节点的所有 `attribute()`，若发现名为 `mode`、类型为 INT、值为 1 的属性，就把 GE 侧的 `mode` 属性置为 `"iof"`，否则保持默认 `"iou"`；最后补齐 `eps` 和 `aligned` 两个默认属性。这段代码展示了 ONNX 属性三要素的判断写法：`attr.name()`（属性名）、`attr.type()`（属性类型）、`attr.i()`（整型值）。

注册语句本体见 [common/src/framework/npu_iou_onnx_plugin.cpp:L36-L44](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/framework/npu_iou_onnx_plugin.cpp#L36-L44)：注册 GE 算子名为 `"Iou"`，`OriginOpType` 列出了从私有域 `npu::1::NPUIou` 到 `ai.onnx::11` ~ `ai.onnx::18` 共 9 个版本串——这意味着无论用户模型用哪个 opset 导出，都能命中这条映射。`ImplyType::TVM` 声明执行交给算子编译框架，插件不做任何计算。

同目录下其他插件都是这个骨架的变奏，例如 `npu_nms_with_mask_onnx_plugin.cpp` 在 [common/src/framework/npu_nms_with_mask_onnx_plugin.cpp:L42-L50](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/framework/npu_nms_with_mask_onnx_plugin.cpp#L42-L50) 以同样的套路把 `NPUNmsWithMask` 注册成 GE 算子 `NMSWithMask`；`yolo_onnx_plugin.cpp` 在 [common/src/framework/yolo_onnx_plugin.cpp:L60-L68](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/framework/yolo_onnx_plugin.cpp#L60-L68) 注册检测后处理算子 `Yolo`。

#### 4.1.4 代码实践

1. **实践目标**：数出 `common/src/framework` 下所有插件注册的 GE 算子名清单。
2. **操作步骤**：在仓库根目录执行 `grep -n 'REGISTER_CUSTOM_OP' common/src/framework/*.cpp objdetect/*/framework/*.cpp`。
3. **需要观察的现象**：每个文件恰好一处（roi_align_onnx_plugin.cpp 有两处，原因见 4.4）；注册名与文件名大多对应（`npu_iou_onnx_plugin.cpp` → `"Iou"`），但也有例外（roi_align 注册的是 `"PartitionedCall"`）。
4. **预期结果**：得到约 25 条注册记录，形成一张「插件文件 → GE 算子名」对照表。本实践为纯源码阅读，无需 NPU 环境。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `OriginOpType` 要列出 9 个版本串，而不是只写一个 `ai.onnx::11::NPUIou`？

**答案**：ONNX 模型导出时的 opset 版本由用户的训练/导出环境决定，插件无法控制。GE 匹配 `OriginOpType` 是精确字符串匹配，`ai.onnx::11::NPUIou` 和 `ai.onnx::12::NPUIou` 是不同的串。把所有预期版本都列出来，才能保证各版本导出的模型都能命中映射、正常迁移。

**练习 2**：`ImplyType::TVM` 如果理解错了，以为插件里要写核函数，会犯什么错误？

**答案**：插件文件里没有任何计算代码，只有属性翻译。`ImplyType::TVM` 的语义是「该 GE 算子的设备实现由算子编译框架按算子信息生成」，真正的 kernel 在 op_kernel/op_host 或 CANN 内置算子库里。插件里写计算逻辑既不会被执行，也没有执行的上下文。

### 4.2 onnx_common.h：插件公共工具箱

#### 4.2.1 概念说明

所有插件都要做几件重复的杂事：打印日志前取算子名、把内存数据包装成 `ge::Tensor`、修改输入输出的格式描述。`common/inc/framework/onnx_common.h` 把这些杂事收拢成 4 个内联工具，避免 25 个插件各写一份。它同时统一了插件所需的头文件集合（GE 注册接口、graph 对象、日志、ONNX protobuf 生成头），插件源文件只需 `#include "onnx_common.h"` 一行即可开工。

#### 4.2.2 核心流程

四个工具的分工：

| 工具 | 输入 → 输出 | 用途 |
| --- | --- | --- |
| `GetOpName(op)` | 任意算子对象 → `std::string` | 日志里标注是哪个算子报的错 |
| `Vec2Tensor(vals, dims, dtype)` | `vector<T>` → `ge::Tensor` | 把一组数值包成带 shape 的 Tensor，常用于构造 Const 节点 |
| `CreateScalar(val, dtype)` | 单个值 → 标量 Tensor | 构造标量常量（如 concat 维度） |
| `ChangeFormatFromOnnx(op, idx, fmt, is_input)` | 算子 + 端口序号 | 强制改写某输入/输出端口的 format 描述 |

#### 4.2.3 源码精读

`GetOpName` 见 [common/inc/framework/onnx_common.h:L32-L42](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/framework/onnx_common.h#L32-L42)：它是函数模板，无论传入 `ge::Operator` 还是其他带 `GetName` 的类型都能工作；取名失败时返回字符串 `"None"` 而不是空串，保证后续 `OP_LOGE` 拼接不出空指针/空字段。

`Vec2Tensor` 与 `CreateScalar` 见 [common/inc/framework/onnx_common.h:L44-L62](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/framework/onnx_common.h#L44-L62)：两者都是先构造 `TensorDesc`（shape + format + dtype），再用 `reinterpret_cast` 把数据指针和字节数交给 `ge::Tensor`。注意 `ge::Tensor` 只持有指针不拷贝数据，所以调用方必须保证 vector 在 Tensor 使用期间存活。

`ChangeFormatFromOnnx` 见 [common/inc/framework/onnx_common.h:L64-L86](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/inc/framework/onnx_common.h#L64-L86)：按 `is_input` 分两路，分别对第 `idx` 个输入或输出端口，把 `Format` 和 `OriginFormat` 同时设为目标格式并 `UpdateInputDesc`/`UpdateOutputDesc` 写回。ONNX 侧语义上是 NCHW，而某些底层算子实现约定特定格式，这个工具就在翻译阶段把格式描述对齐（4.4.3 的 psroi_poolingV2 会用到）。

#### 4.2.4 代码实践

1. **实践目标**：统计公共工具在插件中的实际复用情况。
2. **操作步骤**：执行 `grep -l 'ChangeFormatFromOnnx' common/src/framework/*.cpp objdetect/*/framework/*.cpp` 与 `grep -l 'CreateScalar' objdetect/*/framework/*.cpp`。
3. **需要观察的现象**：`ChangeFormatFromOnnx` 被哪些插件调用、作用在第几号端口、改成什么格式。
4. **预期结果**：会发现 psroi_poolingV2 等少数插件使用了格式改写，而 roi_align 的子图改写用到了 `CreateScalar`（见 4.4.2）。纯源码阅读实践。

#### 4.2.5 小练习与答案

**练习**：`CreateScalar` 里 `vector<int64_t> dims_scalar = {}` 构造的是什么 shape？为什么 concat 的维度参数要用标量 Tensor？

**答案**：空 dims 构造的是标量（0 维、shape 为 `[]`）的 TensorDesc。GE 的 `Concat` 算子要求 `concat_dim` 是一个 Const 节点输入，其值必须是标量整型，所以插件用 `CreateScalar(int32值, ge::DT_INT32)` 生成一个标量 Tensor 再挂到 `op::Const` 节点上（见 4.4.2 的 `dim_const_op`）。

### 4.3 插件源的组织与编译链路

#### 4.3.1 概念说明

仓库里的插件源分布在两级：

- **`common/src/framework/`**：跨算子共享的插件族，主要是 ONNX 标准域/私有域算子（iou、nms、yolo 系列、psroi_pooling、upsample、trans_argb 等）。
- **`<算子>/framework/`**：只服务单个算子的插件，如 `objdetect/roi_align/framework/`。放在算子目录里的好处是插件与算子交付件一起维护、一起评审。

两级目录的插件最终汇入**同一个编译目标**，这点从 CMakeLists 就能看出来。

#### 4.3.2 核心流程

编译链路全貌：

```
common/src/framework/CMakeLists.txt / <算子>/framework/CMakeLists.txt
        │  add_onnx_plugin_sources() / add_tf_plugin_sources()
        ▼
file(GLOB *_onnx_plugin.cpp / *_tf_plugin.cpp)     ← 按文件名后缀自动收集
        │
        ▼
oponnx_plugin_<pkg>_obj / optf_plugin_<pkg>_obj    ← OBJECT 库（名字来自 variables.cmake）
        │  gen_onnx_plugin_symbol()（symbol.cmake，整包 gen_norm_symbol 时调用）
        ▼
liboponnx_plugin_<pkg>.so                          ← SHARED 库，安装进算子包
```

#### 4.3.3 源码精读

公共插件目录的构建入口只有两行，见 [common/src/framework/CMakeLists.txt:L11-L12](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/framework/CMakeLists.txt#L11-L12)：`add_onnx_plugin_sources()` 与 `add_tf_plugin_sources()`。算子内目录同理，见 [objdetect/roi_align/framework/CMakeLists.txt:L11](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/framework/CMakeLists.txt#L11) 只调用了 `add_onnx_plugin_sources()`——因为该目录下的 TF 插件尚未纳入自动收集（原因见下）。

收集宏见 [cmake/func.cmake:L907-L913](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L907-L913)：`file(GLOB ONNX_PLUGIN_SRCS ${SOURCE_DIR}/*_onnx_plugin.cpp)` 按命名约定收集当前目录全部 onnx 插件源，然后 `target_sources` 追加进 OBJECT 目标。**这意味着新增一个 ONNX 插件只需新建 `xxx_onnx_plugin.cpp` 文件，无需改任何构建脚本**——与根 CMakeLists「见 CMakeLists.txt 即收集子目录」的思路一脉相承（回顾 u1-l2）。

OBJECT 目标的创建见 [cmake/func.cmake:L854-L901](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L854-L901)：`add_onnx_plugin_modules` 先用 CANN 包里的 `ge_onnx.proto` 生成 protobuf 头（插件要读 `NodeProto` 就靠它），再建 `oponnx_plugin_<pkg>_obj` 目标并固定 C++14 标准，最后链接 protobuf 静态库与 json 库。

TF 插件一侧刻意保守，见 [cmake/func.cmake:L959-L969](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L959-L969)：`add_tf_plugin_sources` 开头就有两个早退条件——非 `BUILD_WITH_INSTALLED_DEPENDENCY_CANN_PKG`（本地源码依赖构建）或开启了 `ENABLE_TEST` 都直接返回，不收集任何 TF 源。配套的全局开关在 [cmake/variables.cmake:L40](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/variables.cmake#L40)：`ENABLE_AUTO_TF_PLUGIN_SOURCES` 默认 `OFF`。目前公共目录里唯一的 TF 源是占位文件，见 [common/src/framework/cv_stub_tf_plugin.cpp:L19-L24](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/framework/cv_stub_tf_plugin.cpp#L19-L24)，其注释明确说明：这个文件只为保持 TF 插件目标可编译，真正的适配器要通过 `*_tf_plugin.cpp` 逐步加入。

最终产物由 [cmake/symbol.cmake:L486-L518](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/symbol.cmake#L486-L518) 的 `gen_onnx_plugin_symbol` 生成：把 OBJECT 目标包成 SHARED 库 `oponnx_plugin_<pkg>`，用 `--whole-archive` 链接 `rt2_registry_static` 以保留注册宏生成的静态初始化代码（否则链路器会裁掉「没人引用」的注册对象），并安装到 `ONNX_PLUGIN_LIB_INSTALL_DIR`。该函数在整包构建的 `gen_norm_symbol` 汇总函数中被调用（[cmake/symbol.cmake:L576-L578](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/symbol.cmake#L576-L578)），注意自定义算子链路（`gen_cust_symbol`）不生成插件库——框架插件目前只在整包交付时随包发布。

#### 4.3.4 代码实践

1. **实践目标**：验证「新增插件文件即自动参与编译」的约定。
2. **操作步骤**：在 `common/src/framework/` 下新建一个空文件 `my_probe_onnx_plugin.cpp`（只写一行 `// probe`），然后在本地配套 CANN 环境执行 `./build.sh --pkg --soc Ascend910B1`（或你环境对应的芯片型号）并观察编译日志；完成后**删除该探针文件**（不要把探针留在工作区）。
3. **需要观察的现象**：编译日志中出现 `my_probe_onnx_plugin.cpp` 被编入 `oponnx_plugin_*_obj` 目标的记录。
4. **预期结果**：无需改任何 CMakeLists，探针文件即被 GLOB 收集。若本地无编译环境，可改用源码阅读方式验证：对照 [cmake/func.cmake:L907-L913](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L907-L913) 的 GLOB 模式 `*_onnx_plugin.cpp`，逐个检查目录内文件名是否匹配，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么链接 `rt2_registry_static` 时要用 `-Wl,--whole-archive`？

**答案**：`REGISTER_CUSTOM_OP` 生成的注册代码位于全局对象的构造函数里，没有任何函数显式调用它。链接器的默认策略会丢弃「未被引用」的目标文件成员，注册代码就会被裁掉，导致插件注册表为空、模型解析时全部算子匹配失败。`--whole-archive` 强制保留包内全部成员，注册构造才能在库加载时执行。

**练习 2**：算子内 `framework/` 目录的插件和 `common/src/framework/` 的插件最终是编到一起还是分开的？

**答案**：编到一起。两级目录的 CMakeLists 都调用 `add_onnx_plugin_sources()`，源码经 GLOB 后 `target_sources` 追加进同一个 `${ONNX_PLUGIN_NAME}_obj` OBJECT 目标，最终合成一个 `liboponnx_plugin_<pkg>.so`。区别只在源码的仓库归属和维护评审单位。

### 4.4 算子内 framework 目录：roi_align 的三种进阶手法

#### 4.4.1 概念说明

`objdetect/roi_align/framework/` 是本讲的最佳进阶样本，它在一个目录里集中了三种超出「属性翻译」范畴的手法：

1. **`ParseOpToGraphFn` 子图改写**：ONNX 的 `RoiAlign` 是三输入（features、rois、batch_indices），而底层 GE 算子 `ROIAlign` 是两输入（features、拼接后的 rois）。插件在翻译阶段直接改图：插入 Unsqueeze/Cast/Concat 一串辅助算子完成输入整形。
2. **按 opset 双注册**：opset 16 起 RoiAlign 新增 `coordinate_transformation_mode` 属性，两个注册项分别处理新旧语义，映射到不同的 `roi_end_mode` 默认值。
3. **TF 插件的 `FusionParseParamsFn`**：TensorFlow 侧同名算子在融合场景下属性缺失，插件负责补默认值。

#### 4.4.2 核心流程

`ParseOpToGraphRoiAlign` 构造的子图：

```
data2(batch_indices) ─Unsqueeze(-1)─ Cast(float) ┐
                                                 ├─ Concat(dim=1) ──► ROIAlign ──► 输出
data1(rois) ──────────────── Cast(float) ────────┘     ▲
data0(features) ──────────────────────────────────────┘
```

即先把 batch_indices 升维、转 float，与 rois 拼成 (n,5) 布局，再喂给两输入的 `ROIAlign` 算子——这正是 u5-l1 讲过的「rois 形状为 (n,5)，第 0 列是 box 索引」契约在模型迁移侧的来源。

#### 4.4.3 源码精读

属性提取与动态端口注册见 [objdetect/roi_align/framework/roi_align_onnx_plugin.cpp:L21-L55](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/framework/roi_align_onnx_plugin.cpp#L21-L55)：公共函数 `OpRoiAlignUpdateInfo` 遍历 ONNX 属性取出 output_height/output_width/sampling_ratio/spatial_scale，映射为 GE 属性 pooled_height/pooled_width/sample_num/spatial_scale；末尾 `DynamicInputRegister("x", input_size)` 按 ONNX 节点实际输入个数注册动态输入端口——ONNX 侧输入数不定时必须这样做，静态注册会漏端口。

两代 opset 的差异化解析见 [objdetect/roi_align/framework/roi_align_onnx_plugin.cpp:L57-L91](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/framework/roi_align_onnx_plugin.cpp#L57-L91)：`ParseParamsRoiAlign`（opset 8~15）把 `roi_end_mode` 默认置 0；`ParseParamsRoiAlignV16`（opset 16+）默认置 2，只有当 `coordinate_transformation_mode` 为 `"output_half_pixel"` 时才改回 0——同一语义参数在不同 opset 下的对齐约定不同，插件负责归一。

子图改写本体见 [objdetect/roi_align/framework/roi_align_onnx_plugin.cpp:L93-L144](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/framework/roi_align_onnx_plugin.cpp#L93-L144)：先从 GE 算子读回前一步存的属性，再创建三个 `op::Data` 输入锚点；用 4.2 讲的 `CreateScalar` 造出值为 1 的 int32 标量 Const 作为 concat 维度；随后依次建 Unsqueeze（axes=-1）、两个 Cast（转 float）、Concat（动态两输入）节点；最后建 `op::ROIAlign` 节点挂上全部属性，`graph.SetInputs(...).SetOutputs(...)` 声明子图边界。注意这里建的辅助算子（Unsqueeze/Cast/Concat）都是 GE 内置算子，不需要插件关心它们的实现。

两次注册见 [objdetect/roi_align/framework/roi_align_onnx_plugin.cpp:L147-L163](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/framework/roi_align_onnx_plugin.cpp#L147-L163)：两条 `REGISTER_CUSTOM_OP("PartitionedCall")` 各自绑定一段 `OriginOpType`（opset 8~15 与 16~18）和对应的解析函数，且都挂了 `ParseOpToGraphFn(ParseOpToGraphRoiAlign)`。GE 算子名用 `"PartitionedCall"` 而非 `"RoiAlign"`，因为这个 ONNX 节点实际以函数调用形式出现在模型里；同目录 [objdetect/roi_align/framework/npu_roi_align_onnx_plugin.cpp:L62-L70](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/framework/npu_roi_align_onnx_plugin.cpp#L62-L70) 则把私有域的 `NPURoiAlign` 注册为 GE 算子 `"ROIAlign"`，两条通道并存。

TF 插件见 [objdetect/roi_align/framework/roi_align_tf_plugin.cpp:L19-L33](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/objdetect/roi_align/framework/roi_align_tf_plugin.cpp#L19-L33)：`ROIAlignParams` 使用的是 `FusionParseParamsFn`（融合场景回调）而非 `ParseParamsFn`，入参也从单个 `Message*` 变成 `vector<const Message*>`（一个融合组多个节点）；它只做一件事——为属性缺失的 TF 节点补上 spatial_scale=1.0、pooled 7×7 的默认值。结合 4.3 所述，这类 `*_tf_plugin.cpp` 目前不会被自动收集编译，属于预留适配层。

最后看本轮唯一被修改的文件 psroi_poolingV2。它的特殊点在于属性来源：不是从 `NodeProto` 逐个读，而是从 GE 算子上一个名为 `"attribute"` 的 JSON 字符串属性整体解析，见 [common/src/framework/psroi_poolingV2_onnx_plugin.cpp:L24-L51](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/framework/psroi_poolingV2_onnx_plugin.cpp#L24-L51)：用 `op_src.GetAttr("attribute", attrs_string)` 取出 JSON 串，`json::parse` 后遍历 `attrs["attribute"]` 数组，按 `name` 字段挑出 spatial_scale（`f` 浮点字段）、output_dim 与 group_size（`i` 整型字段）并计数；凑不满 `ATTR_NUM = 3` 个就报错返回（L49 的日志文案在 394ba763 提交中由中文顿号改为英文逗号，语义不变）。随后见 [common/src/framework/psroi_poolingV2_onnx_plugin.cpp:L52-L62](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/framework/psroi_poolingV2_onnx_plugin.cpp#L52-L62)：设置三个 GE 属性后，连续三次调用 `ChangeFormatFromOnnx` 把两个输入和一个输出的格式统一改为 NCHW——这是 4.2 公共工具的典型消费者。注册语句见 [common/src/framework/psroi_poolingV2_onnx_plugin.cpp:L65-L73](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/framework/psroi_poolingV2_onnx_plugin.cpp#L65-L73)，注意它用的是 `ParseParamsByOperatorFn`（入参为 `ge::Operator&`，因为属性已经在 GE 算子上）而非 `ParseParamsFn`（入参为 protobuf `Message*`）。

#### 4.4.4 代码实践

1. **实践目标**：梳理 `npu_iou_onnx_plugin.cpp` 的「注册接口—属性解析—下游算子」映射，写一段调用说明。
2. **操作步骤**：
   - 阅读 [common/src/framework/npu_iou_onnx_plugin.cpp:L16-L44](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/framework/npu_iou_onnx_plugin.cpp#L16-L44)；
   - 在仓库中检索 GE 算子名 `"Iou"` 的下游：`grep -rn '"Iou"' --include='*.cpp' --include='*.h' objdetect/ common/`，找到消费这些属性（mode/eps/aligned）的算子实现或 L0 封装；
   - 按 4.1.1 的链路图写一段 200 字左右的说明，覆盖：匹配哪些 ONNX 类型串、翻译出哪些属性及默认值、执行交给谁。
3. **需要观察的现象**：插件设置的属性名与下游算子（如 ciou/iou 相关算子的 def 或 L0 封装）期望的属性名是否一一对应。
4. **预期结果**：得到一份映射说明文档草稿。属性对齐情况如无法在本地完全确认（下游可能在 CANN 包内置算子中），明确标注「下游实现位于 CANN 内置算子库，待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：roi_align 的 ONNX 插件为什么要建 `op::Data` 节点并 `set_attr_index`？

**答案**：`ParseOpToGraphFn` 要求插件产出一个完整的子图，子图的输入边界必须用 `op::Data` 节点声明。`set_attr_index(0/1/2)` 指明该 Data 锚点对应原算子的第几个输入端口，GE 由此把外部连线正确接到子图内部。

**练习 2**：`ParseParamsFn`、`ParseParamsByOperatorFn`、`ParseOpToGraphFn`、`FusionParseParamsFn` 四个回调分别什么场合用？

**答案**：`ParseParamsFn` 最常见，从 ONNX 的 protobuf `NodeProto` 读属性（npu_iou、roi_align）；`ParseParamsByOperatorFn` 用于属性已经过初步转换、挂在 `ge::Operator` 上的场景（psroi_poolingV2 从 JSON 字符串属性解析）；`ParseOpToGraphFn` 在需要把一个节点展开为多算盘子图时使用（roi_align 的三输入转两输入）；`FusionParseParamsFn` 服务 TF 融合场景，一次拿到融合组内的多个节点并补默认属性（roi_align_tf_plugin）。

**练习 3**：psroi_poolingV2 插件为什么在最后要改两次 0 号输入的格式（`ChangeFormatFromOnnx(op_dest, 0, FORMAT_NCHW, true/false)`）？

**答案**：同一个函数里第 0 号输入被改了 `is_input=true` 一次、`is_input=false` 时改的其实是第 0 号**输出**（`ChangeFormatFromOnnx` 的最后一个参数区分输入/输出端口）。三连调用的真实语义是：输入 0、输入 1、输出 0 全部统一为 NCHW，与底层 PSROIPoolingV2 实现的格式约定对齐。

## 5. 综合实践

**任务：为 `psroi_poolingV2_onnx_plugin.cpp` 写一份完整的「插件适配卡片」。**

把本讲四个模块的知识串起来，产出一张结构化卡片，包含以下字段：

1. **身份信息**：插件文件路径、注册的 GE 算子名（`PSROIPoolingV2`）、匹配的 ONNX 类型串集合（`ai.onnx::8` ~ `ai.onnx::16` 共 9 个版本，见 [common/src/framework/psroi_poolingV2_onnx_plugin.cpp:L65-L73](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/common/src/framework/psroi_poolingV2_onnx_plugin.cpp#L65-L73)）。
2. **解析方式**：`ParseParamsByOperatorFn` + JSON 字符串属性，画出「`attribute` JSON 串 → json::parse 遍历 → 三个 GE 属性」的数据流。
3. **格式约定**：列出三处 `ChangeFormatFromOnnx` 调用各自作用的端口与目标格式。
4. **编译归属**：说明该文件如何经 `add_onnx_plugin_sources()` 进入 `oponnx_plugin_<pkg>_obj`，最终成为 `liboponnx_plugin_<pkg>.so` 的一部分（引用 [cmake/func.cmake:L907-L913](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/func.cmake#L907-L913) 与 [cmake/symbol.cmake:L486-L518](https://github.com/gitcode.com/cann/ops-cv/blob/394ba763c277cbe076d44b35d80bef8f901af18e/cmake/symbol.cmake#L486-L518)）。
5. **对照参考**：与 npu_iou（最简模式）和 roi_align（子图改写模式）各列两条差异。

完成后把卡片存入你自己的学习笔记（不要写入仓库源码目录）。

## 6. 本讲小结

- 框架插件是模型解析器（domi）的适配层，只做「ONNX/TF 节点 → GE 算子」的名字映射与属性翻译，不做任何计算；`ImplyType::TVM` 声明执行交给算子编译框架。
- 插件标准骨架是 `REGISTER_CUSTOM_OP(...).FrameworkType(...).OriginOpType({...}).ParseParamsFn(...).ImplyType(...)`，`OriginOpType` 必须穷举预期 opset 版本串才能覆盖各版本模型。
- `common/inc/framework/onnx_common.h` 提供 GetOpName/Vec2Tensor/CreateScalar/ChangeFormatFromOnnx 四件公共工具，是消除插件样板代码的关键。
- 两级插件源（`common/src/framework` 与算子内 `framework/`）通过 `add_onnx_plugin_sources()` 的 `file(GLOB *_onnx_plugin.cpp)` 自动收集进同一 OBJECT 目标，新增插件零构建脚本改动；TF 插件侧目前默认关闭自动收集，仅有桩文件。
- 进阶手法三种：`ParseOpToGraphFn` 子图改写（roi_align 三输入转两输入）、按 opset 双注册归一语义差异、TF `FusionParseParamsFn` 补融合默认属性；psroi_poolingV2 则展示了 `ParseParamsByOperatorFn` + JSON 属性解析与格式统一。
- 本轮（394ba763）对本讲范围的修改仅一处日志文案：psroi_poolingV2 的 `OP_LOGE` 分隔符由中文顿号改为英文逗号，无行为变化。

## 7. 下一步学习建议

- 下一讲进入 u7 单元（专家层）：建议先学 u7-l1 单元测试体系，把「读插件」升级为「验证插件」——思考如何为一个属性翻译函数构造带各种属性组合的 NodeProto 用例。
- 继续阅读源码：对照 `common/src/framework/npu_giou_onnx_plugin.cpp`、`npu_ciou_onnx_plugin.cpp` 与 `npu_iou_onnx_plugin.cpp`，观察同一族度量算子的插件差异；再读 `yolo` 系列插件体会复杂多属性算子的翻译。
- 若想打通端到端认知，可结合 u2-l4 的 GE 图模式：插件产出的 GE 算子与 `op_graph` 目录注册的原型（u6-l1）共同决定了图模式下算子能否被识别与执行。
- 有配套环境时，尝试用 ATC 工具转换一个包含 RoiAlign 节点的 ONNX 模型，观察插件日志（`OP_LOGE/OP_LOGI` 前缀带 `ONNX_PLUGIN` 子模块名）验证本讲链路。

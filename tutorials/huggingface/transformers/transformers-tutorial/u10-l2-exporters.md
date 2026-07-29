# 模型导出 Exporters

## 1. 本讲目标

训练或加载得到的 `PreTrainedModel` 是一个「活在 PyTorch 里的 `nn.Module`」，它依赖 PyTorch 运行时、动态图、Python 解释器。但在真实部署场景里，目标设备可能是 ONNX Runtime、TensorRT、手机上的 ExecuTorch，甚至是 AOT 编译后的独立程序——它们都不认识 `nn.Module`。**导出（export）就是把同一个模型定义，翻译成这些目标运行时能消费的静态计算图。**

本讲学完后，你应当能够：

- 说清 transformers 为什么把导出器放在库内部，而不是丢给下游库；
- 理解 `HfExporter` 抽象基类定义的统一 `export` 接口，以及 ONNX / Dynamo / ExecuTorch 三种后端的差异与协作；
- 掌握 `OnnxConfig` 等 `ExportConfigMixin` 配置数据类的继承体系与关键字段；
- 会用 `AutoExportConfig` + `AutoHfExporter` 的「自动分发」范式，按一份字典选择并实例化正确的导出器；
- 能把一个小模型导出为 ONNX，并用 ONNX Runtime 跑一次推理验证结果与原模型一致。

## 2. 前置知识

本讲依赖你已经建立的认知（见 `u5-l2` PreTrainedModel 模型基类）：

- **`PreTrainedModel`**：所有 PyTorch 模型的基类，是 `nn.Module` 的子类，导出器的输入就是它。
- **`from_pretrained`**：加载一个 checkpoint 得到 `PreTrainedModel` 实例的方式（本讲直接复用，不再展开）。

下面补充几个本讲要用到的、与「导出」相关的通用概念，初学者可能不熟悉：

- **计算图（graph）与追踪（trace/tracing）**：PyTorch 默认是「动态图」，每次前向都现搭现拆。导出的核心动作是**追踪**——用一个样例输入跑一次前向，把过程中执行过的运算记录成一张**静态的有向无环图**（节点是算子，边是张量）。导出后的程序就由这张图 + 权重组成，不再需要 Python 解释器。
- **`torch.export`**：PyTorch 2.x 官方推荐的导出 API（前身是 TorchScript / `torch.jit.trace`）。它把模型追踪成一个 `ExportedProgram` 对象。transformers 的 `DynamoExporter` 直接调用它。
- **动态形状（dynamic shapes）**：追踪时用到的样例输入是固定形状（如 `batch=2, seq=7`），但部署时往往要支持任意长度。`dynamic=True` 会把张量的每个维度标记为「符号维度（symbolic dim）」，使导出图接受任意尺寸输入而无需重新追踪。
- **ONNX**：一种跨框架的开放计算图格式（ protobuf 文件）。`.onnx` 文件可被 ONNX Runtime、TensorRT、OpenVINO 等多种运行时加载。
- **ExecuTorch**：PyTorch 官方的「端侧/边缘」部署栈，目标设备是手机、嵌入式，产物是 `.pte` 文件。

> **关于「实验性（experimental）」**：官方文档明确警告，导出器目前是实验性的，其中大量代码是在打补丁绕开上游（Torch / ONNX Script / ONNX Runtime / ExecuTorch）的 bug，等上游修复后这些补丁会被移除。本讲引用的源码行号基于当前 HEAD（`dfff6dc70d`），是准确的；但补丁本身在未来版本会增减，这一点请在阅读源码时心中有数。

## 3. 本讲源码地图

导出器是 `src/transformers/` 下的一个**子系统目录** `exporters/`，共 8 个文件、约 3800 行。本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [exporters/configs.py](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/configs.py) | 定义 `ExportFormat` 枚举与 `ExportConfigMixin` 配置基类，以及 `DynamoConfig` / `OnnxConfig` / `ExecutorchConfig` 三个具体配置。 |
| [exporters/base.py](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/base.py) | 抽象基类 `HfExporter`：统一 `export` / `export_for_generation` 接口与环境校验。 |
| [exporters/exporter_dynamo.py](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_dynamo.py) | `DynamoExporter`：基于 `torch.export` 的「基座」导出器，产出 `ExportedProgram`。 |
| [exporters/exporter_onnx.py](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_onnx.py) | `OnnxExporter`：继承 `DynamoExporter`，先拿到 `ExportedProgram` 再用 `torch.onnx.export` 降级为 ONNX。 |
| [exporters/exporter_executorch.py](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_executorch.py) | `ExecutorchExporter`：同样继承 `DynamoExporter`，产出 `.pte` 的 `ExecutorchProgramManager`。 |
| [exporters/auto.py](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/auto.py) | `AutoExportConfig` / `AutoHfExporter` 自动分发，以及 `register_exporter` / `register_export_config` 扩展装饰器。 |
| [exporters/utils.py](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/utils.py) | 补丁/修复注册表（`_PATCHES`、`_FX_*_FIXES`）、`prepare_for_export`、`decompose_for_generation` 等工具。 |

> **本次增量说明**：触发本讲更新的提交 `[docs] Exporters` **重写了** `docs/source/en/exporters.md`，并**新增**了 `docs/source/en/exporters_extend.md`（已加入 `_toctree.yml`）与 `docs/source/en/main_classes/exporters.md` 两篇文档。但 `src/transformers/exporters/` 源码本身在此次变更中**没有任何改动**，因此核心概念（`HfExporter` / `AutoHfExporter` / `OnnxConfig` / `ExportFormat`）保持不变，本讲只是把讲解与重写后的官方文档对齐。

## 4. 核心概念与源码讲解

### 4.1 导出体系总览：ExportFormat 与 ExportConfigMixin

#### 4.1.1 概念说明

先建立全局心智模型：transformers 的导出体系回答两个问题——

1. **「导出到哪种格式？」**：由一个枚举 `ExportFormat` 表达，目前有三种——`DYNAMO`（`torch.export` 的 `ExportedProgram`）、`ONNX`、`EXECUTORCH`。三种后端各自的产物与适用运行时如下（摘自重写后的官方文档）：

   | 导出器 | 产物 | 目标运行时 |
   | --- | --- | --- |
   | `DynamoExporter` | `ExportedProgram` | 任意 PyTorch 运行时、AOT 编译 |
   | `OnnxExporter` | `ONNXProgram` | ONNX Runtime、TensorRT、OpenVINO |
   | `ExecutorchExporter` | `ExecutorchProgramManager` | 移动与边缘设备（ExecuTorch） |

2. **「导出时用什么参数？」**：由配置数据类表达。所有配置都继承自 `ExportConfigMixin`，它带一个必备字段 `export_format`，用来在**序列化往返（save/load）**时识别「这是哪种后端的配置」。

为什么导出器要放在 transformers 库内部、而不是交给下游库？官方文档的回答很直白：放在库内，模型架构的任何改动（新的注意力模式、新的 `Cache` 类型）都能在导出时**第一时间**被支持，因为导出器与建模代码同仓、同版本发布。

#### 4.1.2 核心流程

一条最小的导出调用链是「**导出器（exporter）+ 配置（config）+ 样例输入（sample_inputs）**」三件套：

```
config = OnnxConfig(dynamic=True)          # 1. 选后端 + 设参数
exporter = OnnxExporter()                  # 2. 实例化对应导出器（构造时校验环境）
program  = exporter.export(model, inputs, config=config)   # 3. 追踪并产出目标格式
```

`ExportFormat` 枚举只在「需要序列化 / 需要自动分发」时才被读取——它本质上是配置类的一个**类型标签（type tag）**，让一份 `dict`（或磁盘上的 `export_config.json`）能在反序列化时被路由回正确的配置子类。

#### 4.1.3 源码精读

`ExportFormat` 是一个非常简单的枚举，三个值一一对应三种后端，注释点明它的用途是「存进配置、用于序列化往返」：

```python
class ExportFormat(Enum):
    """Identifies the export backend. Stored in ExportConfigMixin for serialisation round-trips."""
    EXECUTORCH = "executorch"
    DYNAMO = "dynamo"
    ONNX = "onnx"
```
参见 [exporters/configs.py:27-32](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/configs.py#L27-L32)（定义三种导出格式的枚举）。

`ExportConfigMixin` 是所有配置的基类，它提供 `to_dict` / `from_dict` 实现序列化往返，并要求子类带一个 `export_format` 字段：

```python
@dataclass
class ExportConfigMixin:
    export_format: ExportFormat

    @classmethod
    def from_dict(cls, config_dict):
        config = cls(**config_dict)
        return config

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.__dict__)
```
参见 [exporters/configs.py:35-69](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/configs.py#L35-L69)（配置基类与序列化方法）。注意两点：① 它是 `@dataclass`，字段即参数；② `to_dict` 做了 `deepcopy`，所以改返回值不会影响配置实例本身。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`export_format` 是序列化往返的钥匙」。

**操作步骤**（示例代码，非项目原有）：

```python
from transformers.exporters.configs import OnnxConfig, ExportFormat

cfg = OnnxConfig(dynamic=True)
d = cfg.to_dict()
print(d["export_format"])          # ExportFormat.ONNX
print(OnnxConfig.from_dict(d))     # 重建出等价的 OnnxConfig
```

**需要观察的现象**：`to_dict()` 后 `export_format` 仍是枚举值 `ExportFormat.ONNX`；`from_dict` 能无报错地重建。

**预期结果**：`d` 是一个普通 `dict`，可用 `json` 落盘；`from_dict(d)` 得到的对象与原 `cfg` 字段一致。**待本地验证**（需要可导入的 transformers 环境）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ExportFormat` 要做成枚举，而不是直接用字符串 `"onnx"`？
**答案**：枚举能杜绝拼写错误（如 `"ONNX"` / `"Onnx"`），并在 IDE 中获得自动补全；同时它仍是可序列化的（值是字符串），在 `to_dict`/`from_dict` 与自动分发时既可当枚举也可当字符串，见 4.4 节的兼容处理。

**练习 2**：`ExportConfigMixin.from_dict` 直接 `cls(**config_dict)`，如果 `config_dict` 里多了一个子类不认识的键会怎样？
**答案**：因为是普通 `@dataclass`（非 `**kwargs`），多出的键会触发 `TypeError: unexpected keyword argument`。这正是下游 `AutoExportConfig.from_dict` 要先按 `export_format` 选对子类再 `from_dict` 的原因——选对了子类，键就对得上。

---

### 4.2 HfExporter 抽象基类与三种导出后端

#### 4.2.1 概念说明

`HfExporter` 是**所有导出器的抽象基类（ABC）**，定义在 [exporters/base.py:42](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/base.py#L42)。它本身不能实例化（`export` 是 `@abstractmethod`），子类必须实现 `export`。它的职责有三块：

1. **环境校验**：构造时检查后端依赖是否安装、版本是否达标。
2. **统一 `export` 接口**：签名固定为 `export(model, sample_inputs, config)`，返回后端特定的产物。
3. **生成式导出 `export_for_generation`**：把一个会自回归生成的模型拆成 prefill / decode 等多个组件，逐个导出。

关键的设计是**继承层次**：`DynamoExporter` 是「基座」，直接继承 `HfExporter`；`OnnxExporter` 和 `ExecutorchExporter` **都继承 `DynamoExporter`**——它们不是从零各自追踪，而是**先复用 `DynamoExporter` 拿到一个 `ExportedProgram`，再各自降级**到 ONNX 或 ExecuTorch。这避免了三种后端重复实现「如何把 transformers 模型喂给 `torch.export`」这段最麻烦的逻辑。

#### 4.2.2 核心流程

**DynamoExporter.export 的五阶段**（源码里以 `# ── Stage N: … ──` 注释块标注）：

```
1. 前向签名补丁  —— 把 model.forward 的 **kwargs 拍平成显式签名，避免 torch.export 把输入捆成一团
2. 模型补丁      —— 用 apply_patches("dynamo") 临时替换不可追踪的模型方法（如 dtype 强转）
3. Pytree 注册   —— 注册 Cache / ModelOutput 等类型，让 torch.export 能展平/重建它们
4. 动态形状      —— dynamic=True 时给每个张量与 cache 叶子分配 Dim.AUTO
5. 状态清理      —— 重置模型在 forward 里设置、却被 torch.export 留成 fake tensor 的属性
   ── 以上都在一个 with 上下文里，最终调用 torch.export.export(...) 得到 ExportedProgram
```

**OnnxExporter.export** 在此之上又包了一层：先 `super().export(...)` 拿到 `ExportedProgram`，再对图做 FX 节点修复，最后用 `torch.onnx.export` 降级成 `ONNXProgram`。

**export_for_generation** 的思路：自回归生成在 prefill 步（整段 prompt、无 KV cache）和 decode 步（单 token、有 KV cache）的输入形状完全不同，不能用一张静态图覆盖。于是导出器跑一次 `model.generate(**inputs, max_new_tokens=2)`，用钩子（hook）**抓取真实的 prefill / decode 前向参数**，再分别导出。多模态模型还会把 prefill 进一步拆成 `image_encoder` / `language_model` / `lm_head` 等组件。

#### 4.2.3 源码精读

`HfExporter` 的环境校验由三个类属性驱动，构造函数调用 `validate_environment`：

```python
class HfExporter(ABC):
    required_packages: list[str] = []          # 必须安装的依赖
    min_versions: dict[str, str] = {}          # 硬性最低版本，低于则报错
    tested_versions: dict[str, str] = {}       # 验证过的版本，不符只告警

    def __init__(self):
        self.validate_environment()
```
参见 [exporters/base.py:42-56](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/base.py#L42-L56)（抽象基类与三组版本属性）。`validate_environment` 会一次性收集所有「缺失」与「版本漂移」再统一上报，而不是遇到第一个就中断，参见 [exporters/base.py:58-98](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/base.py#L58-L98)。

抽象方法 `export` 定义了统一签名与详尽的 docstring：

```python
@abstractmethod
def export(self, model, sample_inputs, config):
    """Export the model and return the backend-specific program object."""
    raise NotImplementedError(...)
```
参见 [exporters/base.py:100-130](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/base.py#L100-L130)（统一导出接口）。注意 `sample_inputs` 是**前向 kwargs**（即 `model(**sample_inputs)` 的参数），不是生成参数；若要导出生成，应改用 `export_for_generation`。

`DynamoExporter` 是基座，它的 `export` 把上面五个阶段组织进一个 `with` 上下文，最后调用 `torch.export.export`：

```python
class DynamoExporter(HfExporter):
    required_packages = ["torch"]
    min_versions = {"torch": "2.11.0"}
    tested_versions = {"torch": "2.12.0"}

    def export(self, model, sample_inputs, config) -> ExportedProgram:
        ...
        with (apply_patches("dynamo"),
              reset_model_state(model),
              patch_model_config(model, output_flags),
              patch_forward_signature(model, sample_inputs)):
            exported_program = torch.export.export(
                model, args=(), kwargs=copy.deepcopy(dict(sample_inputs)),
                strict=config.strict, dynamic_shapes=dynamic_shapes,
                prefer_deferred_runtime_asserts_over_guards=config.prefer_deferred_runtime_asserts_over_guards,
            )
        return exported_program
```
参见 [exporters/exporter_dynamo.py:68-120](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_dynamo.py#L68-L120)（基座导出器：调用 `torch.export`）。五个阶段的注释块分别在 [exporter_dynamo.py:123](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_dynamo.py#L123)（Stage 1）、[:182](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_dynamo.py#L182)（Stage 2）、[:406](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_dynamo.py#L406)（Stage 3）、[:592](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_dynamo.py#L592)（Stage 4）、[:627](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_dynamo.py#L627)（Stage 5）。

`OnnxExporter` 继承 `DynamoExporter`，复用 `super().export(...)` 再降级到 ONNX，体现了「先 Dynamo、再降级」的复用设计：

```python
class OnnxExporter(DynamoExporter):
    required_packages = ["torch", "onnx", "onnxscript"]
    tested_versions = {"torch": "2.12.0", "onnx": "1.21.0", "onnxscript": "0.7.0"}

    def export(self, model, sample_inputs, config) -> ONNXProgram:
        ...
        with patch_model_outputs(model) as (inputs_names, outputs_names), apply_patches("onnx"):
            exported_program = super().export(model, sample_inputs, config=config)   # 复用基座
            apply_fx_node_fixes("onnx", exported_program.graph_module)               # ONNX 专属 FX 修复
            onnx_program = torch.onnx.export(exported_program, ...)                  # 降级为 ONNX
        apply_onnx_ir_fixes(onnx_program)
        return onnx_program
```
参见 [exporters/exporter_onnx.py:87-135](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_onnx.py#L87-L135)（ONNX 导出器：复用基座再降级）。其五阶段注释块分别在 [exporter_onnx.py:175](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_onnx.py#L175)、[:503](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_onnx.py#L503)、[:538](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_onnx.py#L538)、[:700](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_onnx.py#L700)、[:911](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_onnx.py#L911)。

`ExecutorchExporter` 同样继承 `DynamoExporter`，类声明见 [exporters/exporter_executorch.py:90-105](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/exporter_executorch.py#L90-L105)（依赖 `torch` 与 `executorch`）。

#### 4.2.4 代码实践

**实践目标**：观察「先 Dynamo 再降级」的继承复用，以及环境校验在构造时就生效。

**操作步骤**：

1. 阅读源码确认 `OnnxExporter`/`ExecutorchExporter` 的基类都是 `DynamoExporter`（已在上文给出链接）。
2. （可选，待本地验证）在未安装 `onnxscript` 的环境里执行下面的示例代码，观察构造阶段就抛错：

```python
from transformers.exporters import OnnxExporter
OnnxExporter()   # 预期：ImportError，提示安装 onnx / onnxscript
```

**需要观察的现象**：错误在 `OnnxExporter()` 构造时（而非 `export` 调用时）抛出，且提示信息列出 `required_packages` 与 `tested_versions`。

**预期结果**：`validate_environment` 在 `__init__` 中被调用，缺失依赖立即报错。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `OnnxExporter` 要 `super().export(...)` 而不是自己直接调 `torch.onnx.export`？
**答案**：`torch.onnx.export` 现在接收的输入正是 `ExportedProgram`（而不是裸模型）。`super().export` 负责把模型追踪成 `ExportedProgram`（含签名补丁、动态形状、pytree 注册等所有通用工作），`OnnxExporter` 只需在其上叠加「ONNX 专属的图修复 + 降级」。这样三种后端共享追踪逻辑、各自只写差异部分。

**练习 2**：`min_versions` 与 `tested_versions` 的区别是什么？
**答案**：`min_versions` 是**硬门槛**——低于它直接 `raise ImportError`（功能确实缺失）；`tested_versions` 是**软对齐**——不符只 `logger.warning`（实验性补丁可能行为不同，建议用测试过的版本）。参见 [exporters/base.py:49-53](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/base.py#L49-L53)。

---

### 4.3 OnnxConfig 与配置继承体系

#### 4.3.1 概念说明

每种后端有自己的配置数据类，它们组成一条继承链：

```
ExportConfigMixin            （基类：export_format + to_dict/from_dict）
      ↑
DynamoConfig                （dynamic / strict / dynamic_shapes / prefer_deferred_runtime_asserts_over_guards）
      ↑
   ┌───┴────────────┐
OnnxConfig      ExecutorchConfig
(output_path/   (backend=
 opset_version/  "xnnpack"|"cuda")
 external_data/
 optimize/...)
```

注意一个**反直觉但合理**的设计：`OnnxConfig` 与 `ExecutorchConfig` **都继承自 `DynamoConfig`**，而不是直接继承 `ExportConfigMixin`。原因正是 4.2 节的继承复用——既然 ONNX/ExecuTorch 导出器内部都要先走一遍 Dynamo 的 `ExportedProgram`，它们就天然需要 Dynamo 的全部参数（动态形状、strict 等），所以配置也跟着继承。

#### 4.3.2 核心流程

`DynamoConfig` 的核心字段决定了 `torch.export` 的行为：

| 字段 | 默认 | 作用 |
| --- | --- | --- |
| `dynamic` | `False` | `True` 时把所有维度设为符号维度（`Dim.AUTO`），支持任意尺寸输入 |
| `strict` | `False` | `torch.export` 的严格模式：完整符号追踪，能抓更多错但更慢 |
| `dynamic_shapes` | `None` | 精细控制哪些维度动态，**优先级高于 `dynamic`**，直接转交 `torch.export.export` |
| `prefer_deferred_runtime_asserts_over_guards` | `False` | 把数据依赖的形状 guard 转成运行时 assert；多数 LLM 用细粒度 `Dim(min=,max=)` 时需要设 `True` |

动态形状的两种粒度可以这样理解：

- 粗粒度 `dynamic=True`：`torch.export` 自动推断维度之间的关系，省心但不够精确。
- 细粒度 `dynamic_shapes={"input_ids": {0: batch, 1: seq}, ...}`：你显式声明每个维度的符号与上下界，精确但需要 `prefer_deferred_runtime_asserts_over_guards=True` 来避免追踪期因 guard 不成立而失败。

#### 4.3.3 源码精读

`DynamoConfig` 是 `ExportConfigMixin` 的直接子类，关键字段带详尽 docstring：

```python
@dataclass
class DynamoConfig(ExportConfigMixin):
    export_format: ExportFormat = ExportFormat.DYNAMO
    dynamic: bool = False
    strict: bool = False
    dynamic_shapes: dict[str, Any] | None = None
    prefer_deferred_runtime_asserts_over_guards: bool = False
```
参见 [exporters/configs.py:75-106](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/configs.py#L75-L106)（Dynamo 配置）。

`OnnxConfig` 继承 `DynamoConfig`，把 `export_format` 固定为 `ONNX`，并叠加 ONNX 专属字段——其中 `output_path` 为 `None` 时产物只留在内存（`ONNXProgram`），不为 `None` 才写盘：

```python
@dataclass
class OnnxConfig(DynamoConfig):
    export_format: ExportFormat = ExportFormat.ONNX
    output_path: str | PathLike | None = None
    opset_version: int | None = None
    external_data: bool = True      # 大权重存到 .onnx_data 边车文件，突破 2GB protobuf 上限
    optimize: bool = True           # 跑 onnxscript 优化（常量折叠等）
    export_params: bool = True      # 把权重嵌进图；False 则导出无权重图
    keep_initializer_as_inputs: bool = False
```
参见 [exporters/configs.py:109-149](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/configs.py#L109-L149)（ONNX 配置：继承 Dynamo 并叠加输出/优化选项）。`ExecutorchConfig` 同理，多一个 `backend: str = "xnnpack"`（也可 `"cuda"`），见 [exporters/configs.py:152-170](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/configs.py#L152-L170)。

#### 4.3.4 代码实践

**实践目标**：体验 `output_path` 的「内存 vs 落盘」两种模式。

**操作步骤**（示例代码，取自官方文档 `docs/source/en/exporters.md` 的 ONNX 示例，略有裁剪）：

```python
from transformers import AutoModelForCausalLM, AutoTokenizer
from transformers.exporters import OnnxExporter, OnnxConfig

model = AutoModelForCausalLM.from_pretrained("Qwen/Qwen3-0.6B")
tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen3-0.6B")
inputs = tokenizer("Hello, world!", return_tensors="pt")

# 模式 A：产物留在内存
onnx_program = OnnxExporter().export(model, inputs, config=OnnxConfig(dynamic=True))

# 模式 B：直接落盘
OnnxExporter().export(model, inputs, config=OnnxConfig(output_path="model.onnx"))
```

**需要观察的现象**：模式 A 不产生文件、返回 `ONNXProgram` 对象；模式 B 在当前目录生成 `model.onnx`（权重较大时还会有 `model.onnx_data` 边车文件）。

**预期结果**：`external_data=True`（默认）下，大模型的权重会被拆到 `.onnx_data`，主 `.onnx` 文件保持在 2GB protobuf 上限之内。**待本地验证**（需安装 `torch==2.12.0 onnx==1.21.0 onnxscript==0.7.0 onnxruntime`）。

#### 4.3.5 小练习与答案

**练习 1**：如果想让导出的 ONNX 图**不含权重**（运行时再喂数），该设哪个字段？
**答案**：`export_params=False`。此时图里没有权重常量，必须在运行时通过外部方式提供——某些「权重共享 / 按需加载」的部署场景会用它。

**练习 2**：为什么 `OnnxConfig` 要继承 `DynamoConfig` 而不是 `ExportConfigMixin`？
**答案**：因为 `OnnxExporter.export` 内部会调用 `super().export(...)`（即 `DynamoExporter.export`），那一步需要一个 `DynamoConfig`（含 `dynamic` / `strict` / `dynamic_shapes` 等）。配置继承与导出器继承保持一致，保证「先 Dynamo 再降级」链路上每一步都拿得到所需参数。

---

### 4.4 AutoExportConfig 与 AutoHfExporter 自动分发

#### 4.4.1 概念说明

transformers 全库有一套统一的「Auto 范式」（见 `u2-l1`）：用一个工厂类，根据某个**类型标签**自动查映射表、选出正确的具体类。导出体系完全照搬这套范式，提供两个 Auto 类：

- **`AutoExportConfig`**：给一个 `dict`（含 `export_format` 键），自动选出正确的**配置类**并实例化。
- **`AutoHfExporter`**：给一个配置（或配置 dict），自动实例化正确的**导出器**。

这在你「运行时才决定后端」的场景很有用——比如配置来自磁盘或用户输入，代码里不写死 `OnnxExporter`。此外还提供 `register_exporter` / `register_export_config` 两个装饰器，让你注册**自定义后端**，与 `AttentionInterface`（`u6-l5`）的注册思路同源。

#### 4.4.2 核心流程

自动分发的完整链路是「两张映射表 + 两个 Auto 类」：

```
用户字典 {"export_format": "onnx", "dynamic": True}
        │
        ▼  AutoExportConfig.from_dict
查 AUTO_EXPORT_CONFIG_MAPPING["onnx"] → OnnxConfig  → OnnxConfig.from_dict(dict)  得到 config
        │
        ▼  AutoHfExporter.from_config
查 AUTO_EXPORTER_MAPPING["onnx"]      → OnnxExporter → OnnxExporter(**kwargs)     得到 exporter
        │
        ▼  exporter.export(model, inputs, config=config)
得到 ONNXProgram
```

两张映射表的 key 完全对齐（`onnx` / `dynamo` / `executorch`），保证「配置类」与「导出器类」成对存在。`supports_export_format` 充当**守门函数**：发现某个后端只注册了一半（有配置没导出器，或反之），会给出可操作的告警。

#### 4.4.3 源码精读

两张映射表是整个分发的「单一事实来源」：

```python
AUTO_EXPORTER_MAPPING = {
    "executorch": ExecutorchExporter,
    "dynamo": DynamoExporter,
    "onnx": OnnxExporter,
}
AUTO_EXPORT_CONFIG_MAPPING = {
    "executorch": ExecutorchConfig,
    "dynamo": DynamoConfig,
    "onnx": OnnxConfig,
}
```
参见 [exporters/auto.py:27-37](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/auto.py#L27-L37)（导出器与配置的映射表）。

`AutoExportConfig.from_dict` 读 `export_format`，兼容「枚举值或字符串」两种写法，再委托具体配置类的 `from_dict`：

```python
class AutoExportConfig:
    @classmethod
    def from_dict(cls, export_config_dict: dict):
        export_format = export_config_dict.get("export_format")
        if export_format is None:
            raise ValueError("export_config_dict must contain key 'export_format' ...")
        name = export_format.value if isinstance(export_format, ExportFormat) else export_format
        if name not in AUTO_EXPORT_CONFIG_MAPPING:
            raise ValueError(f"Unknown exporter type, got {name} ...")
        target_cls = AUTO_EXPORT_CONFIG_MAPPING[name]
        return target_cls.from_dict(export_config_dict)
```
参见 [exporters/auto.py:42-67](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/auto.py#L42-L67)（按字典自动选配置类）。

`AutoHfExporter.from_config` 先用 `supports_export_format` 守门，再查表实例化导出器：

```python
class AutoHfExporter:
    @classmethod
    def from_config(cls, export_config, **kwargs) -> HfExporter:
        export_config_dict = export_config.to_dict() if isinstance(export_config, ExportConfigMixin) else export_config
        if not cls.supports_export_format(export_config_dict):
            raise ValueError(f"Unsupported export config: {export_config_dict!r}. ...")
        export_format = export_config_dict["export_format"]
        name = export_format.value if isinstance(export_format, ExportFormat) else export_format
        return AUTO_EXPORTER_MAPPING[name](**kwargs)
```
参见 [exporters/auto.py:76-88](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/auto.py#L76-L88)（按配置自动实例化导出器）。守门函数 `supports_export_format` 见 [exporters/auto.py:122-157](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/auto.py#L122-L157)。

注册自定义后端用两个装饰器，它们会校验类型并把类塞进映射表（重复注册会告警覆盖）：

```python
def register_exporter(name: str):
    def register_exporter_fn(cls):
        ...
        if not issubclass(cls, HfExporter):
            raise TypeError("Exporter must extend HfExporter")
        AUTO_EXPORTER_MAPPING[name] = cls
        return cls
    return register_exporter_fn
```
参见 [exporters/auto.py:160-181](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/auto.py#L160-L181)（`register_exporter` 与 `register_export_config`）。注意 `AutoHfExporter.from_pretrained` 目前还是**占位实现**（`raise NotImplementedError`），规划中的「导出配方（export recipe）」工作流尚未落地，见 [exporters/auto.py:90-120](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/auto.py#L90-L120)——不要在代码里调用它。

#### 4.4.4 代码实践

**实践目标**：用「自动分发」范式，从一份纯字典导出 ONNX（对比 4.3 节手写 `OnnxExporter` 的写法）。

**操作步骤**（示例代码，取自官方文档 `docs/source/en/exporters.md` 的 Auto 示例）：

```python
from transformers.exporters import AutoExportConfig, AutoHfExporter

export_config_dict = {"export_format": "onnx", "dynamic": True}
config = AutoExportConfig.from_dict(export_config_dict)   # → OnnxConfig 实例
exporter = AutoHfExporter.from_config(config)             # → OnnxExporter 实例

onnx_program = exporter.export(model, inputs, config=config)
```

**需要观察的现象**：全程没有出现 `OnnxExporter` / `OnnxConfig` 字样，只靠字典里的 `"onnx"` 字符串就选对了后端。

**预期结果**：`type(config).__name__` 为 `OnnxConfig`，`type(exporter).__name__` 为 `OnnxExporter`，与手写方式等价。把字典里的 `"onnx"` 换成 `"dynamo"`，会自动改用 `DynamoExporter`/`DynamoConfig`。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：如果传一个 `{"export_format": "mybackend"}` 的字典，会发生什么？
**答案**：`supports_export_format` 发现 `mybackend` 既不在 `AUTO_EXPORT_CONFIG_MAPPING` 也不在 `AUTO_EXPORTER_MAPPING`，打印「Unknown export format」告警并返回 `False`；随后 `from_config` 抛 `ValueError("Unsupported export config ...")`。

**练习 2**：如何让 `AutoHfExporter` 认识一个全新的后端 `"mybackend"`？
**答案**：写一个继承 `HfExporter` 的导出器、一个继承 `ExportConfigMixin` 的配置，分别用 `@register_exporter("mybackend")` 和 `@register_export_config("mybackend")` 注册。两个都要注册，否则 `supports_export_format` 会告警「只注册了一半」并返回 `False`。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个端到端任务：**导出一个小模型为 ONNX，用 ONNX Runtime 跑推理，并与原模型对比结果；再用 Auto 范式重写一遍。**

**任务步骤**（示例代码，整合自官方文档 `docs/source/en/exporters.md`）：

1. **准备环境**：按文档安装 `pip install transformers "torch==2.12.0" "onnx==1.21.0" "onnxscript==0.7.0" onnxruntime`（版本漂移会有告警，但通常仍可用）。

2. **加载模型与输入**：

   ```python
   from transformers import AutoModelForCausalLM, AutoTokenizer
   import torch

   model = AutoModelForCausalLM.from_pretrained("Qwen/Qwen3-0.6B")
   tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen3-0.6B")
   inputs = tokenizer("Hello, world!", return_tensors="pt")
   ```

3. **用原模型跑一次前向，记录 logits 作为基准**：

   ```python
   with torch.no_grad():
       ref_logits = model(**inputs).logits
   ```

4. **手写方式导出 ONNX 并落盘**：

   ```python
   from transformers.exporters import OnnxExporter, OnnxConfig
   OnnxExporter().export(model, inputs, config=OnnxConfig(output_path="model.onnx", dynamic=True))
   ```

5. **用 ONNX Runtime 加载并推理，对比结果**：

   ```python
   import onnxruntime as ort
   session = ort.InferenceSession("model.onnx")
   ort_inputs = {k: v.numpy() for k, v in inputs.items()}
   ort_logits = session.run(None, ort_inputs)[0]
   print("最大绝对误差:", abs(torch.tensor(ort_logits) - ref_logits).max().item())
   ```

6. **改用 Auto 范式重写第 4 步**（验证结果完全等价）：

   ```python
   from transformers.exporters import AutoExportConfig, AutoHfExporter
   cfg = AutoExportConfig.from_dict({"export_format": "onnx", "dynamic": True})
   exporter = AutoHfExporter.from_config(cfg)
   program = exporter.export(model, inputs, config=cfg)   # 留在内存的 ONNXProgram
   ```

**需要观察的现象**：
- 第 5 步的最大绝对误差应非常小（数值精度量级，如 `1e-4` 以内），证明导出保真。
- 第 6 步与第 4 步产物类型一致（都是 ONNX），只是构造方式不同。

**预期结果**：导出保真，ONNX Runtime 输出与原模型 logits 数值吻合；Auto 范式与手写范式等价。**待本地验证**（受环境与上游 bug 影响，个别模型可能命中 `ONNX_DISABLE_OPTIMIZE` 列表，见下方延伸阅读）。

> **延伸阅读（本次新增文档）**：若导出报错，多半是命中了上游 bug。重写后的 [docs/source/en/exporters.md](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/docs/source/en/exporters.md) 的「Limitations and workarounds」一节列了常见情况（如 FlashAttention 不可导出、需切 `sdpa`）；新增的 [docs/source/en/exporters_extend.md](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/docs/source/en/exporters_extend.md) 讲解了 patch / fix 两类补丁与各后端的 Stage 参考；跳过导出或关闭优化的模型名单则在 [tests/exporters/test_export.py](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/tests/exporters/test_export.py) 的 `EXPORT_SKIPS`（L58）与 `ONNX_DISABLE_OPTIMIZE`（L266）两个字典里。

## 6. 本讲小结

- 导出器放在 `src/transformers/exporters/`，与建模代码同仓同版本，保证架构变更能第一时间被导出支持；目前是**实验性**的，含大量绕开上游 bug 的补丁。
- `ExportFormat` 枚举（onnx/dynamo/executorch）是配置序列化往返的**类型标签**；所有配置继承 `ExportConfigMixin`，提供 `to_dict`/`from_dict`。
- `HfExporter` 是抽象基类，统一 `export(model, sample_inputs, config)` 接口，并在构造时用 `required_packages`/`min_versions`/`tested_versions` 做环境校验；`export_for_generation` 把生成式模型拆成 prefill/decode 等组件分别导出。
- 三种后端是**继承复用**关系：`DynamoExporter`（基座，调 `torch.export`）← `OnnxExporter`/`ExecutorchExporter`（先复用基座拿到 `ExportedProgram`，再各自降级）；配置链 `DynamoConfig ← OnnxConfig/ExecutorchConfig` 与之同构。
- `OnnxConfig` 的关键字段：`output_path`（None 留内存 / 路径落盘）、`dynamic`、`external_data`、`optimize`、`opset_version`。
- `AutoExportConfig` + `AutoHfExporter` 复用全库 Auto 范式，靠两张对齐的映射表（`AUTO_EXPORTER_MAPPING` / `AUTO_EXPORT_CONFIG_MAPPING`）按 `export_format` 自动选类；`register_exporter`/`register_export_config` 支持注册自定义后端。

## 7. 下一步学习建议

- **阅读重写后的官方文档**：先通读 [docs/source/en/exporters.md](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/docs/source/en/exporters.md)（用法与三种后端示例），再读新增的 [docs/source/en/exporters_extend.md](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/docs/source/en/exporters_extend.md)（patch/fix 补丁机制与各 Stage 参考），最后看 API 清单 [docs/source/en/main_classes/exporters.md](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/docs/source/en/main_classes/exporters.md)。
- **结合 `u6` 注意力体系**：导出对注意力后端有要求——FlashAttention/FlexAttention 不可导出，需切 `sdpa` 或 `eager`；可回看 `u6-l1`（掩码）与 `u6-l5`（AttentionInterface）理解为何如此。
- **结合 `u6-l3` KV Cache**：`export_for_generation` 的 decode 组件依赖 KV cache 的形状契约，阅读 `cache_utils.py` 与 [exporters/utils.py:747](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/src/transformers/exporters/utils.py#L747) 的 `decompose_for_generation` 能加深理解。
- **扩展实践**：参照 `exporters_extend.md`，给某个模型写一个最小的 `@register_patch("dynamo", ...)`，观察它如何仅在导出期间替换不可追踪的方法——这是通往「为 transformers 添加导出支持」的入口。
- **测试体系**：阅读 [tests/exporters/test_export.py](https://github.com/huggingface/transformers/blob/dfff6dc70d3fffadf539353743a9e176af8109e9/tests/exporters/test_export.py)，理解 `EXPORT_SKIPS` 与 `ONNX_DISABLE_OPTIMIZE` 两个名单如何标记「暂不可导出」的模型，配合 `u11-l3` 测试体系一起看。

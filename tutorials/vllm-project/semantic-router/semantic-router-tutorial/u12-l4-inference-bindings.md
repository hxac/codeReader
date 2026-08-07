# 推理绑定（candle/ml/nlp/onnx/openvino）

## 1. 本讲目标

本讲聚焦 Semantic Router（SR）的「本地推理引擎」——五个独立的 `*-binding/` 模块。它们把 Rust 或 C/C++ 写的高性能推理能力，通过 CGO 暴露成 Go 可直接调用的函数。学完本讲你应该能够：

1. 说出每个 binding 的 Go 入口签名与职责（candle / onnx / openvino / nlp / ml）。
2. 理解 CGO 绑定的统一模式：C 结构体 → `extern` 声明 → Go 包装函数 → 内存释放纪律。
3. 看懂「后端可替换」是如何通过 `embedding.Provider` 接口缝与 `EMBEDDING_BACKEND_OVERRIDE` 环境变量实现的。
4. 解释 `EMBEDDING_BACKEND_OVERRIDE=candle` 时 `main.go` 做了哪些线程环境调优。

本讲承接 u8-l4（嵌入提供者）建立的「工厂只认远程、本地推理留在工厂之外」的结论：那里被刻意留在工厂之外的本地后端，正是本讲的五个 binding。

## 2. 前置知识

- **CGO 与 FFI**：Go 自带的 CGO 机制让 Go 代码可以调用 C 函数。Rust 可以编译出 C 语言兼容的动态/静态库（`#[repr(C)]` 结构体 + `extern "C"` 函数），因此 Go 通过 CGO 调 Rust 的本质是「Go → C ABI → Rust」。C++（OpenVINO）同理，只是多一层 `extern "C"` 包装。
- **句柄（handle）模式**：跨语言调用时，常用「不透明指针」表示一个在另一侧语言里存活的对象。Go 侧拿到一个 `unsafe.Pointer` 或 `uint64_t`，每次操作把它传回去，最后调一个 `free`/`close` 释放。
- **`#cgo` 指令**：Go 源文件里的 C 代码块顶部用 `#cgo LDFLAGS:` / `#cgo CFLAGS:` 告诉编译器去哪链接库、加哪些头文件路径。
- **构建标签（build constraint）**：文件首行的 `//go:build ...` 注释控制该文件在什么平台/标签下才参与编译，是 SR 实现「可选后端」的关键手段。
- **语义嵌入与近邻检索**：见 u8-l4（Provider 接口）与 u9-l1（HNSW）。本讲的 binding 就是这些能力的本地算力来源。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `candle-binding/semantic-router.go` | 默认本地后端：基于 Rust Candle 的嵌入/相似度/分类/PII/越狱/NLI/多模态，能力最全 |
| `onnx-binding/semantic-router.go` | ONNX Runtime 后端，**镜像 candle 的 API 以便直接替换**（drop-in） |
| `openvino-binding/semantic-router.go` | Intel OpenVINO IR 后端（C++），由 `openvino` 构建标签门控 |
| `nlp-binding/nlp_binding.go` | Rust BM25 + N-gram 关键词分类器，轻量非神经网络 |
| `ml-binding/ml_binding.go` | Rust Linfa 传统机器学习（KNN/KMeans/SVM），仅推理，模型从 JSON 加载 |
| `src/semantic-router/cmd/main.go` | 启动入口，含 `applyBackendRuntimeTuningDefaults()` 线程调优 |
| `src/semantic-router/pkg/extproc/router_selection.go` | 把 candle/openvino 绑进统一 `embedding.Provider` 的接线点 |
| `src/semantic-router/pkg/extproc/openvino_embedding_cgo.go` + `pkg/classification/openvino_backend_cgo.go` / `openvino_backend_stub.go` | openvino 的「cgo 实现 + 桩回退」成对文件 |

> 五个 binding 都是**独立的顶层 Go module**（各自带 `go.mod`），导入路径形如 `github.com/vllm-project/semantic-router/candle-binding`。它们位于仓库根目录而非 `src/` 下，是 monorepo 里的「本地推理资产层」。

## 4. 核心概念与源码讲解

### 4.1 CGO 绑定入口：从 Rust/C 到 Go 的统一模式

#### 4.1.1 概念说明

五个 binding 虽然底层语言不同（四个 Rust + 一个 C++），但 Go 侧的「入口长相」几乎一模一样，遵循同一套 FFI 模式。理解了这一个模式，就能读懂全部五个 binding：

1. **C 类型层**：在 Go 文件的 C 代码块里用 `typedef struct {...}` 定义与 Rust 侧 `#[repr(C)]` 一一对应的结构体（如 `EmbeddingResult`、`ClassifyResult`）。
2. **`extern` 声明层**：声明要调用的 C 函数签名（这些函数由 Rust/C++ 编译出的库导出）。
3. **Go 包装层**：每个 `extern` 函数配一个 Go 函数，负责字符串封送（`C.CString` / `C.GoString`）、调用、结果转换、内存释放。
4. **内存释放纪律**：Rust/C++ 分配的字符串和数组，Go 必须**显式调用对应的 free 函数**（如 `ml_free_string`、`free_classify_result`），否则内存泄漏。

#### 4.1.2 核心流程

一次典型的「Go 调 Rust」往返：

```
Go 调用方
  │  传入 Go string / []float
  ▼
Go 包装函数
  │  C.CString(text)        ← 在 C 堆分配字符串（defer C.free 释放）
  │  组装 C 结构体指针/参数
  ▼
extern C 函数（由 Rust 编译导出）
  │  执行推理，在 Rust 堆分配结果字符串/数组
  │  返回 char* / 结构体指针
  ▼
Go 包装函数
  │  C.GoString(result)     ← 把 C 字符串拷成 Go string
  │  defer C.ml_free_string(result)  ← 释放 Rust 堆内存
  ▼
Go 调用方拿到 []float32 / string
```

关键约束：**跨边界的所有权必须清晰**。`C.CString` 分配的内存由 Go 用 `C.free` 释放；Rust 返回的内存由专门的 `free_*` 函数释放。混用会导致崩溃或泄漏。

#### 4.1.3 源码精读

先看 `ml-binding` 最干净的「句柄 + select + free」三件套（它没有复杂结构体，最适合看模式）：

[candle-binding 的库链接与构建约束](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/candle-binding/semantic-router.go#L1-L6)：candle/nlp/onnx 三个 Rust 后端都用 `!windows && cgo && (amd64 || arm64)` 限制在非 Windows 的 64 位平台且开启 CGO。

```go
#cgo LDFLAGS: -L${SRCDIR}/target/release -lcandle_semantic_router -ldl -lm
```
[candle-binding/semantic-router.go:22-23](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/candle-binding/semantic-router.go#L22-L23) 告诉链接器去 `target/release/` 找 `libcandle_semantic_router` 库（Rust `cargo build --release` 的产物）。`${SRCDIR}` 是 Go 文件所在目录，使绑定模块自包含。

接着看 KNN 的 select，它完整展示了「封送 → 调用 → 转换 → 释放」：

```go
func (s *KNNSelector) Select(query []float64) (string, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.handle == nil {
		return "", errors.New("selector not initialized")
	}
	cQuery := make([]C.double, len(query))
	for i, v := range query {
		cQuery[i] = C.double(v)          // Go float64 → C double
	}
	result := C.ml_knn_select(s.handle, &cQuery[0], C.size_t(len(query)))
	if result == nil {
		return "", errors.New("KNN selection failed")
	}
	defer C.ml_free_string(result)        // 释放 Rust 分配的结果字符串
	return C.GoString(result), nil        // C 字符串 → Go string
}
```
[ml-binding/ml_binding.go:85-105](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/ml-binding/ml_binding.go#L85-L105) 是这套模式的样板：`handle` 是 Rust 侧对象的句柄，`C.ml_free_string` 释放 Rust 堆上的返回值。注意全程用 `sync.RWMutex` 保护句柄——CGO 调用本身不是并发安全的边界，Go 侧自己加锁。

再看 nlp-binding 的结构体往返（Rust 返回复杂结构体的情形）：

```go
typedef struct {
    bool matched;
    char* rule_name;
    char** matched_keywords;
    float* scores;
    int match_count;
    int total_keywords;
} ClassifyResult;
```
[nlp-binding/nlp_binding.go:28-41](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/nlp-binding/nlp_binding.go#L28-L41) 这个 C 结构体与 Rust 侧 `#[repr(C)]` 严格对齐：嵌套的 `char**`（字符串数组）和 `float*`（分数数组）都由 Rust 分配，Go 用 `unsafe.Slice` 把它们当作切片读取，最后用 `free_classify_result` 一次性释放整个结构。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读型实践」验证五个 binding 共享同一 FFI 模式。

1. 打开 `candle-binding/semantic-router.go`、`nlp-binding/nlp_binding.go`、`ml-binding/ml_binding.go`、`onnx-binding/semantic-router.go`、`openvino-binding/semantic-router.go` 各自顶部的 C 代码块。
2. 在每个文件里找：`#cgo LDFLAGS:` 行（链接哪个库）、若干 `extern` 声明、对应的 Go 包装函数。
3. 列一张表，记录每个 binding：链接的库名、是否含结构体、用哪个 free 函数。

**需要观察的现象**：

- candle/onnx/nlp/ml 用 `-L${SRCDIR}/target/release` 链接 Rust 产物（`.dylib`/`.so`）；openvino 用 `-L${SRCDIR}/build` 链接 C++ 产物，并额外用 `#cgo CFLAGS: -I${SRCDIR}/cpp/include` 指定头文件目录（[openvino-binding/semantic-router.go:15-17](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/openvino-binding/semantic-router.go#L15-L17)）。
- 每个返回字符串/数组的调用后面都紧跟一个 `defer C.free_*`。

**预期结果**：你会确认五个 binding 的 Go 入口都是同构的「句柄 + 包装函数 + free」，差别只在链接的库和 C 结构体的复杂度。

> 这些命令需要本地已编译对应原生库（`cargo build --release` 或 CMake）才能运行；当前环境若无 Rust 工具链，标注**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Go 包装函数里几乎每个 `C.xxx_select` 的返回值后面都 `defer C.ml_free_string(result)`，而 `C.CString(text)` 用的是 `defer C.free`？

**答案**：两者所有权归属不同。`C.CString` 是 Go 在 C 堆上分配的内存，用 Go 的 `C.free` 释放即可；而 `ml_knn_select` 的返回值是 **Rust 分配**的（通常用 Rust 的 `CString::into_raw`），必须用 Rust 提供的配对函数 `ml_free_string`（内部 `CString::from_raw`）释放，用 `C.free` 会导致 Rust 内存分配器状态错乱。

**练习 2**：`KNNSelector` 为什么自己持有一把 `sync.RWMutex`？

**答案**：句柄 `s.handle` 是跨 FFI 边界的不透明指针，`Select` 读、`Close` 置 nil；若不加锁，并发 `Select` 与 `Close` 会出现「一个 goroutine 释放了句柄、另一个 goroutine 仍在用」的悬垂指针崩溃。锁把 FFI 调用串行化保护起来。

---

### 4.2 各后端分工：candle / onnx / openvino / nlp / ml

#### 4.2.1 概念说明

五个 binding 并非「同义重复」，而是按**推理任务的算力与成熟度**分工：

- **candle-binding**（默认主力）：基于 Rust 的 Candle 深度学习框架，能力最全——嵌入（Qwen3/Gemma/mmBERT/mmBERT-32K）、相似度、序列分类、PII token 分类、越狱、域/类别、事实核查、反馈、模态、NLI/幻觉、多模态编码。生产运行时主要接的就是它。
- **onnx-binding**（镜像备选）：基于 ONNX Runtime，**刻意把 API 做成与 candle 一致以便直接替换**（drop-in compatibility），但目前**尚未接入非测试的运行时代码**——它是「备胎后端」。
- **openvino-binding**（Intel 路线）：基于 C++ 的 OpenVINO IR（`.xml` 模型），由 `openvino` 构建标签门控，支持设备选择（CPU/GPU/AUTO），面向 Intel 硬件优化。
- **nlp-binding**（轻量词法）：Rust 实现的 BM25 与 N-gram 关键词分类，**不走神经网络**，启动快、开销小，服务于 keyword 信号。
- **ml-binding**（传统机器学习）：Rust Linfa 的 KNN/KMeans/SVM，**只做推理**（训练在 Python `src/training/` 完成），服务于**模型选择**而非请求分类。

一句话总结分工：candle 管深度神经推理，nlp 管词法匹配，ml 管模型选择算法，onnx/openvino 是 candle 的可替换引擎。

#### 4.2.2 核心流程

| 后端 | 底层 | 链接库 | 主入口示例 | 服务对象 | 运行时接入 |
|------|------|--------|-----------|---------|-----------|
| candle | Rust (Candle) | `libcandle_semantic_router` | `GetEmbedding` `ClassifyText` | 嵌入/分类/PII/越狱/多模态 | ✅ 主力 |
| onnx | Rust (ORT) | `lonnx_semantic_router` | `GetEmbedding` `ClassifyTextWithProbabilities` | 同 candle（镜像） | ⚠️ 仅测试代码引用 |
| openvino | C++ (OpenVINO) | `lopenvino_semantic_router` | `GetEmbedding` `GetModernBertEmbedding` | 嵌入/分类 | ✅ 构建标签门控 |
| nlp | Rust | `lnlp_binding` | `NewBM25Classifier().Classify` | keyword 信号 | ✅ keyword_classifier |
| ml | Rust (Linfa) | `lml_semantic_router` | `KNNSelector.Select` | 模型选择 | ✅ modelselection |

candle 的入口面极其宽，覆盖嵌入与多种分类：

```go
func GetEmbedding(text string, maxLength int) ([]float32, error) {
	if !modelInitialized {
		return nil, fmt.Errorf("BERT model not initialized")
	}
	cText := C.CString(text)
	defer C.free(unsafe.Pointer(cText))
	result := C.get_text_embedding(cText, C.int(maxLength))
	if bool(result.error) {
		return nil, fmt.Errorf("failed to generate embedding")
	}
	embedding := cFloatArrayToGoSlice(result.data, result.length)
	return embedding, nil
}
```
[candle-binding/semantic-router.go:657-675](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/candle-binding/semantic-router.go#L657-L675) 是嵌入的 Go 入口：返回 `[]float32`（用 float32 省内存并配合 SIMD，见 u9-l1）。`InitModel` 负责加载 BERT 相似度模型，`ClassifyText`（L2048）负责序列分类。

nlp-binding 的 BM25 入口则更轻：

```go
func NewBM25Classifier() *BM25Classifier {
	handle := C.bm25_classifier_new()
	return &BM25Classifier{handle: handle}
}
```
[nlp-binding/nlp_binding.go:99-102](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/nlp-binding/nlp_binding.go#L99-L102) 创建一个 BM25 分类器句柄；随后用 `AddRule` 逐条加规则（[nlp_binding.go:112](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/nlp-binding/nlp_binding.go#L112)），`Classify` 返回 `MatchResult`（含命中关键词与分数）。

ml-binding 的 Linfa 选择器走的是另一条路——**模型不在 Go 里训练，而是从 Python 训练后导出的 JSON 加载**：

```go
func KNNFromJSON(json string) (*KNNSelector, error) {
	cJSON := C.CString(json)
	defer C.free(unsafe.Pointer(cJSON))
	handle := C.ml_knn_from_json(cJSON)
	if handle == nil {
		return nil, errors.New("failed to load KNN from JSON")
	}
	return &KNNSelector{handle: handle}, nil
}
```
[ml-binding/ml_binding.go:135-145](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/ml-binding/ml_binding.go#L135-L145) 注释明确「Training is done in Python」，绑定只做推理。SVM 还支持核函数选择，RBF 核打分为：

\[ f(x) = \sum_i \alpha_i \cdot \exp(-\gamma \|x - x_i\|^2) \]

#### 4.2.3 源码精读

**onnx 的「镜像」证据**。onnx-binding 的包文档直说：

```go
// Package onnx_binding provides Go bindings for mmBERT ONNX Runtime inference.
// This mirrors the candle_binding API for drop-in compatibility.
```
[onnx-binding/semantic-router.go:6-8](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/onnx-binding/semantic-router.go#L6-L8) 它的 `GetEmbedding(text, maxLength) ([]float32, error)`（[L377-383](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/onnx-binding/semantic-router.go#L377-L383)）与 candle 同名同参。但 `InitModel` 只是转发到 mmBERT 嵌入初始化（[L297-299](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/onnx-binding/semantic-router.go#L297-L299)），`InitClassifier` 把 `numClasses` 形参**直接丢弃**（`_ int`，[L329-331](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/onnx-binding/semantic-router.go#L329-L331)）——因为 ONNX 用「具名槽位」（`intent`/`jailbreak`/`generic`…）而非类别数定位模型，这是它与 candle 的细微语义差。

**重要事实**：在 `src/semantic-router/` 的非测试 Go 代码中，**没有任何一处 `import onnx_binding`**（仅 `*_test.go` 引用）。也就是说 onnx-binding 目前是「API 就绪、运行时未接线」的备选后端，真正跑在生产里的是 candle（主力）与 openvino（标签门控）。

**openvino 的设备抽象**。与 candle 的 `useCPU bool` 不同，openvino 用字符串设备名：

```go
func InitModel(modelPath string, device string) error
```
[openvino-binding/semantic-router.go:127](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/openvino-binding/semantic-router.go#L127) 接受 `"CPU"`/`"GPU"`/`"AUTO"`，且模型路径指向 OpenVINO IR 的 `.xml` 文件而非 HF 模型目录。`GetModernBertEmbedding`（[L821](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/openvino-binding/semantic-router.go#L821)）提供 ModernBERT 嵌入。

#### 4.2.4 代码实践

**实践目标**：对比 candle-binding 与 onnx-binding 的 Go 入口签名，验证「镜像」并找出差异。

1. 用 Grep 在两个文件里搜 `^func ` 列出全部导出函数。
2. 把同名函数对齐成表（`InitModel`、`GetEmbedding`、`ClassifyText` vs `ClassifyTextWithProbabilities`、`InitClassifier`）。
3. 对每对函数，标注：参数是否一致？返回类型是否一致？语义是否一致？

**需要观察的现象与预期结果**：

| 能力 | candle-binding | onnx-binding | 一致性 |
|------|----------------|--------------|--------|
| 嵌入 | `GetEmbedding(text string, maxLength int) ([]float32, error)`（L657） | `GetEmbedding(text string, maxLength int) ([]float32, error)`（L377） | ✅ 完全一致 |
| 初始化 | `InitModel(modelID string, useCPU bool) error`（L558，加载 BERT） | `InitModel(modelID string, useCPU bool) error`（L297，转发到 mmBERT） | ⚠️ 签名一致、语义不同 |
| 序列分类 | `ClassifyText(text string) (ClassResult, error)`（L2048） | 只有 `ClassifyTextWithProbabilities`（L675） | ⚠️ 命名/粒度不同 |
| 通用分类器 | `InitClassifier(modelPath, numClasses int, useCPU)`（L1933） | `InitClassifier(modelPath, _ int, useCPU)`（L329，丢弃 numClasses） | ⚠️ 签名一致、忽略参数 |

**结论**：两者签名高度同构（drop-in 的基础），但 onnx 用「具名槽位 + 概率分布」模型，candle 用「类别数 + 单标签」模型，语义并非逐字等价——这正是 onnx 能「替换」但要小心语义对齐的原因。

> 若本地有 ONNX Runtime 与模型，可把 `router_selection.go` 里 `candle_binding.GetEmbeddingBatched` 临时换成 `onnx_binding` 等价物编译验证；否则标注**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 onnx-binding 的 `InitClassifier` 第二个参数写成 `_ int`（下划线）？

**答案**：为了与 candle 的 `InitClassifier(modelPath, numClasses, useCPU)` 保持**相同签名**以实现 drop-in，但 ONNX 后端不需要类别数（它用 `intent`/`jailbreak`/`generic` 这类具名槽位定位已编译好的模型），所以用 `_` 显式忽略，告诉阅读者「这个参数在这里没用」。

**练习 2**：nlp-binding 和 candle-binding 都做「分类」，本质区别是什么？

**答案**：nlp-binding 的 BM25/N-gram 是**词法统计**方法（基于词频与 n-gram 相似度，不跑神经网络，轻量、确定性），服务于 keyword 这类规则型信号；candle-binding 跑的是**深度神经网络**（BERT/ModernBERT），输出概率分布，服务于 domain/jailbreak/pii 这类学习型信号（见 u2-l2 的信号族分类）。

---

### 4.3 后端可替换：统一接口缝与环境变量

#### 4.3.1 概念说明

SR 的设计哲学是：**Go 内核不应直接依赖任何一个本地推理后端**。否则换引擎（candle→onnx→openvino）就要改遍业务代码。于是它用两层抽象把后端「拔插化」：

1. **统一接口缝**：`pkg/embedding` 的 `Provider` 接口（u8-l4）+ `NewFuncProvider` 适配器，把「任何 `func(context, string) ([]float32, error)`」都升级成 Provider。本地后端通过它接入，而不实现整个接口。
2. **解析优先级**：一个环境变量 `EMBEDDING_BACKEND_OVERRIDE` 决定运行期用哪个后端，优先级为「环境变量 > 配置显式 backend > `model_type=="remote"` > 兜底 candle」。
3. **构建标签门控**：openvino 这种重依赖后端用 `openvino` 构建标签 + 桩文件（stub）实现「不编译就不存在」。

#### 4.3.2 核心流程

请求链路里，模型选择阶段需要嵌入时的后端解析（在 `resolveSelectionEmbeddingProvider` 里）：

```
读 EMBEDDING_BACKEND_OVERRIDE（归一化小写）
  │ 为空则取 config.EmbeddingModels.EmbeddingBackend()
  ▼
switch backend
  ├─ openai_compatible → embedding.NewProvider(...)        远程 HTTP 后端
  ├─ openvino          → openvinoEmbeddingFunc + NewFuncProvider  本地 C++
  └─ default(candle)   → candle_binding.GetEmbedding* + NewFuncProvider  本地 Rust
```

注意：分类信号用的 candle 后端（域/PII/越狱等）是**直接在 classification 包里 import** 的（见 4.2.3 的 init 站点），不走 Provider 缝；只有「嵌入」这条公共能力才被统一抽象成 Provider，供选择算法、工具检索、启动探活复用（u8-l4）。

#### 4.3.3 源码精读

**接线点**：把 candle 绑进 Provider 的就是 `router_selection.go`：

```go
func resolveSelectionEmbeddingProvider(cfg *config.RouterConfig) (embedding.Provider, error) {
	backend := embedding.BackendOverrideFromEnv()
	if backend == "" {
		backend = cfg.EmbeddingModels.EmbeddingBackend()
	}
	if backend == config.EmbeddingBackendOpenAICompatible {
		return embedding.NewProvider(cfg.EmbeddingModels, embedding.ProviderOptions{})
	}
	modelType := selectionEmbeddingModelType(cfg, backend)
	switch backend {
	case config.EmbeddingBackendOpenVINO:
		openvinoEmbed := openvinoEmbeddingFunc(modelType)
		return embedding.NewFuncProvider(backend, 0, func(_ context.Context, text string) ([]float32, error) {
			return openvinoEmbed(text)
		})
	default:
		return embedding.NewFuncProvider(config.EmbeddingBackendCandle, selectionEmbeddingDimension(cfg, modelType), func(_ context.Context, text string) ([]float32, error) {
			if modelType == config.EmbeddingModelTypeQwen3 {
				output, err := candle_binding.GetEmbeddingBatched(text, modelType, selectionEmbeddingDimension(cfg, modelType))
				if err != nil { return nil, err }
				return output.Embedding, nil
			}
			output, err := candle_binding.GetEmbeddingWithModelType(text, modelType, 0)
			if err != nil { return nil, err }
			return output.Embedding, nil
		})
	}
}
```
[src/semantic-router/pkg/extproc/router_selection.go:81-113](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/router_selection.go#L81-L113)：这是「后端可替换」的核心。`NewFuncProvider` 把一个闭包封成 Provider，闭包内部才真正 `import candle_binding`。换后端 = 改这个 switch，业务代码零改动。

**openvino 的 cgo/桩成对文件**：

```go
//go:build openvino && !windows && cgo
// openvino_embedding_cgo.go
func openvinoEmbeddingFunc(modelType string) func(string) ([]float32, error) {
	return func(text string) ([]float32, error) {
		switch modelType {
		case "mmbert", "modernbert":
			return openvino_binding.GetModernBertEmbedding(text, 32768)
		default:
			return openvino_binding.GetEmbedding(text, 32768)
		}
	}
}
```
[src/semantic-router/pkg/extproc/openvino_embedding_cgo.go:1-18](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/openvino_embedding_cgo.go#L1-L18) 只有带 `-tags openvino` 编译时才存在。没有这个标签时，编译器选中的是桩文件：

```go
//go:build !openvino || windows || !cgo
// openvino_backend_stub.go
func initOpenVINOModel(...) error {
	return fmt.Errorf("openvino backend requires non-windows build with cgo enabled")
}
```
[src/semantic-router/pkg/classification/openvino_backend_stub.go:1-2](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/openvino_backend_stub.go#L1-L2)（[L16-18](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/classification/openvino_backend_stub.go#L16-L18)）。这对文件用**互斥的构建标签**保证「同一函数名，要么是真实现、要么是报错桩」，使默认构建完全不依赖 OpenVINO 运行时库。这是 Go 处理「可选重依赖」的经典手法。

**main.go 的 candle 线程调优**。`EMBEDDING_BACKEND_OVERRIDE=candle` 时，启动序列会主动设一批线程数环境变量，避免 Rust 嵌入推理与宿主线程库抢核：

```go
func applyBackendRuntimeTuningDefaults() {
	backend := strings.TrimSpace(strings.ToLower(os.Getenv("EMBEDDING_BACKEND_OVERRIDE")))
	if backend != "candle" {
		return
	}
	defaults := map[string]string{
		"OMP_NUM_THREADS":        "1",
		"MKL_NUM_THREADS":        "1",
		"OPENBLAS_NUM_THREADS":   "1",
		"RAYON_NUM_THREADS":      "1",
		"TOKENIZERS_PARALLELISM": "false",
	}
	applied := make(map[string]string)
	for key, value := range defaults {
		if _, exists := os.LookupEnv(key); exists {
			continue          // 用户已显式设置则不覆盖
		}
		_ = os.Setenv(key, value)
		applied[key] = value
	}
	// ... 发一条 backend_runtime_tuning_applied 事件日志
}
```
[src/semantic-router/cmd/main.go:59-94](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L59-L94)：
- 只在 `candle` 后端触发；其他后端（含远程）直接 `return`。
- 五个变量分别管 OpenMP / MKL / OpenBLAS（CPU 线程库）、Rayon（Rust 数据并行库，candle 用它）、tokenizers（HuggingFace 分词器的 fast 模式并行）。
- 用 `os.LookupEnv` 检查，**只填未显式设置的**——尊重用户已有配置。
- 它在 `main()` 第 22 行、紧接日志初始化后调用（[main.go:22](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L22)），确保在任何推理初始化之前线程库已读到正确值。

为什么是「1」？因为 SR 自己用 goroutine 并发处理多请求，若每个请求内 candle 又各开 N 个线程，总线程数会爆炸（M×N），互相抢占导致抖动。统一压到单线程、由 Go 层做并发编排，是更可控的策略。

#### 4.3.4 代码实践

**实践目标**：解释 `EMBEDDING_BACKEND_OVERRIDE=candle` 时 main.go 做了哪些环境调优，并验证后端切换。

1. 阅读 [main.go:59-94](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/cmd/main.go#L59-L94)，列出被设置的五个环境变量及其作用。
2. 阅读 [router_selection.go:81-113](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/semantic-router/pkg/extproc/router_selection.go#L81-L113)，追踪：把 `EMBEDDING_BACKEND_OVERRIDE` 分别设为空、`candle`、`openvino`、`openai_compatible` 时，`resolveSelectionEmbeddingProvider` 各走哪个分支、返回什么 Provider。
3. （可选，需本地编译）用 `EMBEDDING_BACKEND_OVERRIDE=candle go test ./pkg/extproc/...` 观察日志里是否出现 `backend_runtime_tuning_applied` 事件。

**需要观察的现象**：

- 调优只在 `candle` 触发，且只在变量未设置时填默认。
- 后端解析严格按优先级：环境变量覆盖一切。

**预期结果**：你能画一张「环境变量值 → 走哪条 switch 分支 → 用哪个 binding/Provider」的映射表。若本地无 Rust 工具链无法跑测试，明确标注**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果用户已经 `export RAYON_NUM_THREADS=4`，`applyBackendRuntimeTuningDefaults` 会把它改成 1 吗？

**答案**：不会。函数用 `os.LookupEnv(key)` 判断，若变量已存在就 `continue` 跳过，只填**未显式设置**的变量。这是「尊重用户显式配置」的设计。

**练习 2**：为什么 openvino 用「cgo 文件 + 桩文件」成对设计，而 candle 不需要？

**答案**：candle 是默认/主力后端，几乎所有部署都要用，且其构建约束 `!windows && cgo && (amd64||arm64)` 在标准 Linux/macOS 构建中天然满足，不需要额外开关。openvino 依赖 Intel OpenVINO 运行时（重依赖、非普遍存在），用 `openvino` 构建标签 + 桩文件让它「不带标签时完全不存在」，避免默认构建被一个用不上的重依赖拖垮。

**练习 3**：`resolveSelectionEmbeddingProvider` 用 `NewFuncProvider` 把 candle 包进 Provider，这印证了 u8-l4 的哪条结论？

**答案**：印证「工厂只认远程、本地后端被刻意留在工厂之外」。`embedding.NewProvider`（工厂）只处理 `openai_compatible` 远程后端；candle/openvino 这些 CGO 本地后端因依赖 CGO 被排除在纯 Go 的工厂外，由 extproc 用 `NewFuncProvider` 就地拼装，从而保持 `pkg/embedding` 纯 Go。

## 5. 综合实践

**任务**：画出 SR 一次「模型选择阶段需要嵌入」时的本地推理后端解析全景图。

要求：

1. 从环境变量 `EMBEDDING_BACKEND_OVERRIDE` 出发，画出取值（空 / `candle` / `openvino` / `openai_compatible`）→ `resolveSelectionEmbeddingProvider` 的 switch 分支 → 最终调用的 binding 函数（如 `candle_binding.GetEmbeddingBatched`、`openvino_binding.GetModernBertEmbedding`、HTTP 远程）→ 经 `NewFuncProvider` 封装成的统一 `embedding.Provider`。
2. 在图上标注：哪些后端受构建标签门控（openvino 的 `openvino` 标签、candle/nlp/ml/onnx 的 `cgo && (amd64||arm64)`），onnx-binding 为何不在这张图里（未接入运行时）。
3. 单独画一张「启动期线程调优」时序：`main()` → `applyBackendRuntimeTuningDefaults()`（仅 candle）→ 设 5 个 `*_NUM_THREADS=1` 与 `TOKENIZERS_PARALLELISM=false` → 之后才进入模型下载与嵌入初始化。
4. 用一段话说明：如果要把生产后端从 candle 换成 onnx，需要改哪几处（提示：`router_selection.go` 的 switch、各 `*_init.go` 分类器初始化站点），并评估 onnx 当前 API 与 candle 的语义差异带来的风险。

这会把「CGO 入口模式」「后端分工」「可替换接口缝」三块知识串成一条完整链路。

## 6. 本讲小结

- 五个 `*-binding/` 是独立顶层 Go module，通过 CGO 把 Rust（candle/onnx/nlp/ml）与 C++（openvino）推理能力暴露成 Go 函数，共享同一套 FFI 模式：C 结构体 → `extern` 声明 → Go 包装函数 → 显式 free。
- **candle** 是默认主力（嵌入/分类/PII/越狱/多模态/NLI 最全）；**onnx** 镜像 candle API 以便 drop-in，但当前仅测试代码引用、未接运行时；**openvino** 是 Intel IR 路线、由 `openvino` 构建标签门控；**nlp** 是轻量 BM25/N-gram 词法分类；**ml** 是 Linfa KNN/KMeans/SVM，仅推理、模型从 JSON 加载，服务模型选择。
- 后端「可替换」靠两层抽象：`embedding.Provider` + `NewFuncProvider` 接口缝（本地后端就地拼装，`pkg/embedding` 保持纯 Go），与 `EMBEDDING_BACKEND_OVERRIDE` 环境变量优先级（环境变量 > 配置 > remote > 兜底 candle）。
- openvino 用「cgo 文件 + 桩文件」互斥构建标签实现「不编译就不存在」，是处理可选重依赖的 Go 惯用法。
- `EMBEDDING_BACKEND_OVERRIDE=candle` 时，main.go 在日志初始化后、推理初始化前用 `applyBackendRuntimeTuningDefaults()` 把 `OMP/MKL/OPENBLAS/RAYON_NUM_THREADS` 压到 1、关掉 tokenizers 并行，避免 candle 的 Rust 线程与 Go goroutine 抢核；且只填未显式设置的变量。

## 7. 下一步学习建议

- **回头看分类信号的本地后端**：本讲只覆盖了「嵌入」这条公共缝。建议阅读 `pkg/classification/classifier_category_init.go`、`classifier_pii_init.go`、`classifier_jailbreak_init.go`，看 candle 的分类器（`InitClassifier`/`InitPIIClassifier`/`InitJailbreakClassifier`）是如何在 classification 包内**直接 import** 接入的（u8-l2、u8-l3）。
- **训练侧闭环**：`ml-binding` 的模型来自 Python 训练；可读 `src/training/model_selection/ml_model_selection/` 与 u14-l3（训练与评估），理解「Python 训练 → JSON → ml-binding 推理」的完整闭环。
- **部署与构建标签**：结合 u12-l2（Helm/部署）理解 `-tags openvino` 这类构建标签在容器镜像里的传递方式，以及 CGO 依赖如何影响镜像体积与平台矩阵。
- **性能基准**：`openvino-binding/bench/` 与 `candle-binding` 的测试含基准代码，可结合 u14-l3 的 perf/bench 评估不同后端在目标硬件上的延迟与吞吐，作为「选哪个后端」的依据。

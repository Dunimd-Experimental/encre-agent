<span id="7bbc7eed"></span>
# 附6：Function Calling 样本要求
对于 Function Calling 样本，数据格式有以下几点特殊要求：

1. **`tools`** ** 字段 (必填)** ：您必须在样本的顶层提供 `tools` 字段，其格式需与推理 API 的定义保持一致。
2. **`parallel_tool_calls`** ** 字段 (可选)** ：这是一个布尔类型字段，默认值为 `True`。
   * 它用于声明该样本是否支持并行函数调用，其含义与推理 API 中的参数一致。
   * 如果您将此字段设为 `false`，但 `assistant` 的回复中又包含了多个 `tool_calls`，将会导致错误。
3. **`assistant`** ** 消息中的 ** **`tool_calls`** ** 字段**：在 `messages` 列表中，如果 `assistant` 的回复触发了函数调用，则该消息中必须包含 `tool_calls` 字段。
   * `tool_calls` 数组中的每个 `function` 对象都必须包含 `name` 和 `arguments` 两个键。
   * `arguments` 的值必须是一个合法的 JSON 格式字符串。
4. **`tool`** ** 角色的消息**：在 `messages` 列表中，用于表示函数调用返回结果的消息，其 `role` 必须为 `tool`。这类消息的 `loss_weight` 固定为 `0.0`，且不可修改。

以下是一个包含多轮对话和并行函数调用的完整精调样本示例：
```JSON
{
  "messages": [
    {
      "role": "system",
      "content": "你是豆包"
    },
    {
      "role": "user",
      "content": "把北京上海的天气发给Alan"
    },
    {
      "role": "assistant",
      "content": "分别查询北京和上海的天气。",
      "tool_calls": [
        {
          "type": "function",
          "function": {
            "name": "GetCurrentWeather",
            "arguments": "{\"location\": \"北京\"}"
          }
        },
        {
          "type": "function",
          "function": {
            "name": "GetCurrentWeather",
            "arguments": "{\"location\": \"上海\"}"
          }
        }
      ],
      "loss_weight": 1.0
    },
    {
      "role": "tool",
      "content": "北京天气晴，18~25°，微风"
    },
    {
      "role": "tool",
      "content": "上海天气阴，20~30°，大风"
    },
    {
      "role": "assistant",
      "content": "已经查到北京上海天气，需要发送给alan",
      "tool_calls": [
        {
          "type": "function",
          "function": {
            "name": "SendMessage",
            "arguments": "{\"receiver\": \"Alan\", \"content\": \"今天北京天气晴，18~25°，微风；上海天气阴，20~30°，大风\"}"
          }
        }
      ],
      "loss_weight": 1.0
    },
    {
      "role": "tool",
      "content": "发送成功"
    },
    {
      "role": "assistant",
      "content": "已经查到北京上海今日天气，发送给了Alan",
      "loss_weight": 1.0
    }
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "MusicPlayer",
        "description": "歌曲查询Plugin，当用户需要搜索某个歌手或者歌曲时使用此plugin，给定歌手，歌名等特征返回相关音乐",
        "parameters": {
          "properties": {
            "artist": {
              "description": "表示歌手名字",
              "type": "string"
            },
            "description": {
              "description": "表示描述信息",
              "type": "string"
            },
            "song_name": {
              "description": "表示歌曲名字",
              "type": "string"
            }
          },
          "required": [],
          "type": "object"
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "GetCurrentWeather",
        "description": "查询当前的天气",
        "parameters": {
          "type": "object",
          "properties": {
            "location": {
              "type": "string",
              "description": "地理位置，比如北京市"
            },
            "unit": {
              "type": "string",
              "description": "温度单位",
              "enum": [
                "celsius",
                "fahrenheit"
              ]
            }
          },
          "required": [
            "location"
          ]
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "LinkReaderPlugin",
        "description": "当需要解析网页内容时，调用本插件",
        "parameters": {
          "type": "object",
          "properties": {
            "url": {
              "type": "string",
              "description": "需要解析网页链接,最多3个；"
            }
          },
          "required": [
            "url"
          ]
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "WebSearchPlugin",
        "description": "当需要搜索互联网内容时，调用本插件",
        "parameters": {
          "type": "object",
          "properties": {
            "keywords": {
              "type": "string",
              "description": "需要搜索的内容"
            }
          },
          "required": [
            "keywords"
          ]
        }
      }
    },
    {
      "type": "function",
      "function": {
        "name": "SendMessage",
        "description": "需要发送微信消息时，使用本函数",
        "parameters": {
          "type": "object",
          "properties": {
            "receiver": {
              "type": "string",
              "description": "接受对象"
            },
            "content": {
              "type": "string",
              "description": "需要发送的内容"
            }
          },
          "required": [
            "receiver"
          ]
        }
      }
    }
  ]
}
```

在实际的 `.jsonl` 文件中，上述示例应以单行形式呈现：
```Bash
{"messages":[{"role":"system","content":"你是豆包"},{"role":"user","content":"把北京的天气发给Alan"},{"role":"assistant","content":"","tool_calls":[{"type":"function","function":{"name":"GetCurrentWeather","arguments":"{\"location\": \"北京\"}"}}],"loss_weight":1.0},{"role":"tool","content":"北京天气晴，18~25°，微风"},{"role":"assistant","content":"已经查到北京天气，需要发送给alan","tool_calls":[{"type":"function","function":{"name":"SendMessage","arguments":"{\"receiver\": \"Alan\", \"content\": \"今天北京天气晴，18~25°，微风\"}"}}],"loss_weight":1.0},{"role":"tool","content":"发送成功"},{"role":"assistant","content":"已经查到北京今日天气，发送给了Alan","loss_weight":1.0}],"tools":[{"type":"function","function":{"name":"MusicPlayer","description":"歌曲查询Plugin，当用户需要搜索某个歌手或者歌曲时使用此plugin，给定歌手，歌名等特征返回相关音乐","parameters":{"properties":{"artist":{"description":"表示歌手名字","type":"string"},"description":{"description":"表示描述信息","type":"string"},"song_name":{"description":"表示歌曲名字","type":"string"}},"required":[],"type":"object"}}},{"type":"function","function":{"name":"GetCurrentWeather","description":"查询当前的天气","parameters":{"type":"object","properties":{"location":{"type":"string","description":"地理位置，比如北京市"},"unit":{"type":"string","description":"温度单位","enum":["celsius","fahrenheit"]}},"required":["location"]}}},{"type":"function","function":{"name":"LinkReaderPlugin","description":"当需要解析网页内容时，调用本插件","parameters":{"type":"object","properties":{"url":{"type":"string","description":"需要解析网页链接,最多3个；"}},"required":["url"]}}},{"type":"function","function":{"name":"WebSearchPlugin","description":"当需要搜索互联网内容时，调用本插件","parameters":{"type":"object","properties":{"keywords":{"type":"string","description":"需要搜索的内容"}},"required":["keywords"]}}},{"type":"function","function":{"name":"SendMessage","description":"需要发送微信消息时，使用本函数","parameters":{"type":"object","properties":{"receiver":{"type":"string","description":"接受对象"},"content":{"type":"string","description":"需要发送的内容"}},"required":["receiver"]}}}]}
```

**注意事项**

* `tool_calls` 数组中的函数调用数量，必须与后续 `role` 为 `tool` 的消息数量完全对应。
* 您必须遵循 JSON Schema 规范。在函数参数定义中，`type` 字段的值必须是以下类型之一，否则会导致错误：
   * `string`
   * `number`
   * `boolean`
   * `integer`
   * `object`
   * `array`
* 如果您希望模型在返回函数调用（`fc`）的同时输出思考过程（如 CoT、reasoning），请将这部分文本内容填写在 `content` 字段中。
* 如果您的训练样本不需要包含一个完整的函数调用流程，可以进行截断，但样本必须以一个 `role` 为 `assistant` 的消息结尾。

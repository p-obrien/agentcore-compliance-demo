variable "name_prefix" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "grounding_threshold" {
  description = "Contextual grounding score threshold (0-1). Interactions below this route to human review."
  type        = number
  default     = 0.75
}

variable "relevance_threshold" {
  description = "Contextual relevance score threshold (0-1)."
  type        = number
  default     = 0.5
}

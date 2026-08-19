# Build the Model Catalog from account evidence

The Model Catalog will prefer dynamically discovered models and fall back to a maintained Curated Fallback when discovery is unavailable. A successful request for an unknown model creates a timestamped Verified Model record only for the Kiro account that accepted it; it does not mutate the Curated Fallback or imply global availability. Verified Model evidence expires with the account model-cache TTL and is renewed by a later successful call. The catalog exposes one Canonical Model ID per model, while accepted aliases remain hidden from model discovery.

The catalog presents `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` first in that order, followed by the remaining Canonical Model IDs in stable order. Model ownership is reported as Kiro; credit multipliers are informational descriptions and never influence routing.

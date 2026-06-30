from pydantic import BaseModel
from typing import Optional


class Material(BaseModel):
    type_id: int
    type_name: str
    quantity: int
    is_manufactured: bool = False  # True = lze vyrobit, False = primární surovina


class BlueprintActivity(BaseModel):
    time: int  # v sekundách
    materials: list[Material]
    products: list[dict]


class Blueprint(BaseModel):
    type_id: int
    type_name: str
    blueprint_type_id: int
    manufacturing: Optional[BlueprintActivity] = None
    reaction: Optional[BlueprintActivity] = None


class ManufacturingNode(BaseModel):
    type_id: int
    type_name: str
    quantity: int
    depth: int
    children: list["ManufacturingNode"] = []
    is_leaf: bool = False  # True = nelze dál rozkládat (ruda, ice, PI...)


# Resolve the forward reference in `children`. pydantic v2 uses model_rebuild();
# the Android build runs pydantic v1 (no Rust pydantic-core), which uses
# update_forward_refs(). Support both so the same code runs on desktop + Android.
try:
    ManufacturingNode.model_rebuild()        # pydantic v2 (desktop)
except AttributeError:                        # pragma: no cover
    ManufacturingNode.update_forward_refs()   # pydantic v1 (android)

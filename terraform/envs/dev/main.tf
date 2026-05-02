################################################################################
# Dev environment composition.
#
# Module calls are added incrementally as later phases land. The provider's
# `default_tags` already propagates Project/Env/ManagedBy + var.tags down to
# every taggable resource created by the modules below. Each module also
# stamps a per-resource Name and Component tag of its own.
################################################################################

-- Copy and paste this into your Supabase SQL Editor
-- This creates the devices table to hold the FDA catalog
CREATE TABLE public.devices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    primary_di TEXT UNIQUE, -- Unique barcode/UDI
    brand_name TEXT,
    company_name TEXT,
    model_number TEXT,
    catalog_number TEXT,
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Enable Row Level Security (RLS) but make it readable by anyone
ALTER TABLE public.devices ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow public read access to devices" 
ON public.devices FOR SELECT 
USING (true);

-- (Optional but highly recommended) 
-- Add indexes on columns people will search to make lookups instant
CREATE INDEX idx_devices_company_name ON public.devices (company_name);
CREATE INDEX idx_devices_brand_name ON public.devices (brand_name);
CREATE INDEX idx_devices_catalog_number ON public.devices (catalog_number);

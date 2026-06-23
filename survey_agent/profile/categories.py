"""Profile categories — defines all attribute categories and their fields."""

from __future__ import annotations

from pydantic import BaseModel


class ProfileField(BaseModel):
    """A single profile field definition."""

    key: str
    label: str
    field_type: str  # text | number | select | multi_select | boolean | date
    options: list[str] | None = None  # for select/multi_select
    description: str = ""


class ProfileCategory(BaseModel):
    """A category of profile attributes."""

    key: str
    label: str
    description: str
    fields: list[ProfileField]


CATEGORIES: list[ProfileCategory] = [
    ProfileCategory(
        key="demographics",
        label="Demographics",
        description="Basic personal information",
        fields=[
            ProfileField(key="age", label="Age", field_type="number"),
            ProfileField(
                key="gender",
                label="Gender",
                field_type="select",
                options=["Male", "Female", "Non-binary", "Prefer not to say"],
            ),
            ProfileField(
                key="ethnicity",
                label="Ethnicity",
                field_type="select",
                options=[
                    "White",
                    "Black/African American",
                    "Hispanic/Latino",
                    "Asian",
                    "Native American",
                    "Pacific Islander",
                    "Mixed",
                    "Other",
                    "Prefer not to say",
                ],
            ),
            ProfileField(
                key="marital_status",
                label="Marital Status",
                field_type="select",
                options=["Single", "Married", "Divorced", "Widowed", "Separated", "Domestic partnership"],
            ),
            ProfileField(key="zip_code", label="ZIP Code", field_type="text"),
            ProfileField(key="state", label="State", field_type="text"),
            ProfileField(key="city", label="City", field_type="text"),
            ProfileField(key="country", label="Country", field_type="text"),
        ],
    ),
    ProfileCategory(
        key="employment",
        label="Employment",
        description="Work and career information",
        fields=[
            ProfileField(
                key="status",
                label="Employment Status",
                field_type="select",
                options=[
                    "Full-time",
                    "Part-time",
                    "Self-employed",
                    "Unemployed",
                    "Retired",
                    "Student",
                    "Homemaker",
                    "Disabled",
                ],
            ),
            ProfileField(key="industry", label="Industry", field_type="text"),
            ProfileField(key="job_title", label="Job Title", field_type="text"),
            ProfileField(
                key="company_size",
                label="Company Size",
                field_type="select",
                options=["1-10", "11-50", "51-200", "201-500", "501-1000", "1000+"],
            ),
            ProfileField(key="years_experience", label="Years of Experience", field_type="number"),
            ProfileField(
                key="work_remote",
                label="Remote Work",
                field_type="select",
                options=["Fully remote", "Hybrid", "On-site"],
            ),
        ],
    ),
    ProfileCategory(
        key="income",
        label="Income & Finance",
        description="Household income and financial information",
        fields=[
            ProfileField(
                key="household_income",
                label="Household Income Range",
                field_type="select",
                options=[
                    "Under $25,000",
                    "$25,000-$49,999",
                    "$50,000-$74,999",
                    "$75,000-$99,999",
                    "$100,000-$149,999",
                    "$150,000-$199,999",
                    "$200,000+",
                ],
            ),
            ProfileField(
                key="personal_income",
                label="Personal Income Range",
                field_type="select",
                options=[
                    "Under $15,000",
                    "$15,000-$29,999",
                    "$30,000-$49,999",
                    "$50,000-$74,999",
                    "$75,000-$99,999",
                    "$100,000+",
                ],
            ),
            ProfileField(
                key="credit_score_range",
                label="Credit Score Range",
                field_type="select",
                options=["Excellent (750+)", "Good (700-749)", "Fair (650-699)", "Poor (below 650)", "Don't know"],
            ),
            ProfileField(
                key="homeownership",
                label="Housing",
                field_type="select",
                options=["Own", "Rent", "Live with parents/family", "Other"],
            ),
        ],
    ),
    ProfileCategory(
        key="education",
        label="Education",
        description="Educational background",
        fields=[
            ProfileField(
                key="highest_degree",
                label="Highest Degree",
                field_type="select",
                options=[
                    "High school",
                    "Some college",
                    "Associate's",
                    "Bachelor's",
                    "Master's",
                    "Doctorate",
                    "Professional (JD/MD/etc.)",
                    "Trade/vocational",
                ],
            ),
            ProfileField(key="field_of_study", label="Field of Study", field_type="text"),
            ProfileField(key="currently_enrolled", label="Currently Enrolled", field_type="boolean"),
        ],
    ),
    ProfileCategory(
        key="health",
        label="Health & Wellness",
        description="Health-related information",
        fields=[
            ProfileField(
                key="general_health",
                label="General Health",
                field_type="select",
                options=["Excellent", "Very good", "Good", "Fair", "Poor"],
            ),
            ProfileField(key="smoker", label="Smoker", field_type="select", options=["Current", "Former", "Never"]),
            ProfileField(
                key="exercise_frequency",
                label="Exercise Frequency",
                field_type="select",
                options=["Daily", "Several times a week", "Once a week", "A few times a month", "Rarely", "Never"],
            ),
            ProfileField(
                key="conditions",
                label="Health Conditions",
                field_type="multi_select",
                options=["Diabetes", "Heart disease", "Asthma", "Arthritis", "Depression/Anxiety", "None", "Other"],
            ),
            ProfileField(
                key="insurance_type",
                label="Health Insurance",
                field_type="select",
                options=[
                    "Private employer",
                    "Private individual",
                    "Medicare",
                    "Medicaid",
                    "Military/VA",
                    "Uninsured",
                    "Other",
                ],
            ),
        ],
    ),
    ProfileCategory(
        key="shopping",
        label="Shopping & Spending",
        description="Consumer behavior and preferences",
        fields=[
            ProfileField(
                key="online_shopping_frequency",
                label="Online Shopping Frequency",
                field_type="select",
                options=["Daily", "Several times a week", "Weekly", "Monthly", "Rarely", "Never"],
            ),
            ProfileField(
                key="preferred_stores",
                label="Preferred Stores/Brands",
                field_type="multi_select",
                options=["Amazon", "Walmart", "Target", "Costco", "Best Buy", "Other"],
            ),
            ProfileField(
                key="decision_factors",
                label="Purchase Decision Factors",
                field_type="multi_select",
                options=["Price", "Quality", "Brand reputation", "Reviews", "Convenience", "Sustainability", "Other"],
            ),
            ProfileField(
                key="annual_spending_range",
                label="Annual Online Spending",
                field_type="select",
                options=["Under $500", "$500-$1,000", "$1,000-$5,000", "$5,000-$10,000", "$10,000+"],
            ),
        ],
    ),
    ProfileCategory(
        key="politics",
        label="Politics & Social",
        description="Political views and social attitudes",
        fields=[
            ProfileField(
                key="political_affiliation",
                label="Political Affiliation",
                field_type="select",
                options=["Democrat", "Republican", "Independent", "Libertarian", "Green", "Other", "None"],
            ),
            ProfileField(
                key="political_views",
                label="Political Leaning",
                field_type="select",
                options=["Very liberal", "Liberal", "Moderate", "Conservative", "Very conservative"],
            ),
            ProfileField(
                key="social_issues",
                label="Social Issues Interest Level",
                field_type="select",
                options=["Very interested", "Somewhat interested", "Not very interested", "Not at all interested"],
            ),
        ],
    ),
    ProfileCategory(
        key="household",
        label="Household",
        description="Household composition and details",
        fields=[
            ProfileField(key="household_size", label="Household Size", field_type="number"),
            ProfileField(key="children_count", label="Number of Children", field_type="number"),
            ProfileField(
                key="children_ages",
                label="Children Ages",
                field_type="text",
                description="Comma-separated ages, e.g. 3,7,12",
            ),
            ProfileField(
                key="pets",
                label="Pets",
                field_type="multi_select",
                options=["Dog", "Cat", "Bird", "Fish", "Reptile", "None", "Other"],
            ),
        ],
    ),
    ProfileCategory(
        key="technology",
        label="Technology",
        description="Tech usage and preferences",
        fields=[
            ProfileField(
                key="primary_device",
                label="Primary Device",
                field_type="select",
                options=["Desktop", "Laptop", "Tablet", "Smartphone"],
            ),
            ProfileField(
                key="os_preference",
                label="OS Preference",
                field_type="select",
                options=["Windows", "macOS", "Linux", "iOS", "Android", "ChromeOS"],
            ),
            ProfileField(
                key="social_media",
                label="Social Media Platforms",
                field_type="multi_select",
                options=[
                    "Facebook",
                    "Instagram",
                    "X/Twitter",
                    "TikTok",
                    "LinkedIn",
                    "Reddit",
                    "YouTube",
                    "Snapchat",
                    "None",
                ],
            ),
            ProfileField(
                key="streaming_services",
                label="Streaming Services",
                field_type="multi_select",
                options=[
                    "Netflix",
                    "Hulu",
                    "Disney+",
                    "HBO Max",
                    "Amazon Prime",
                    "YouTube Premium",
                    "Spotify",
                    "Apple TV+",
                    "None",
                ],
            ),
            ProfileField(
                key="tech_comfort",
                label="Tech Comfort Level",
                field_type="select",
                options=["Expert", "Advanced", "Intermediate", "Beginner"],
            ),
        ],
    ),
    ProfileCategory(
        key="lifestyle",
        label="Lifestyle",
        description="General lifestyle and interests",
        fields=[
            ProfileField(
                key="hobbies",
                label="Hobbies/Interests",
                field_type="multi_select",
                options=[
                    "Reading",
                    "Gaming",
                    "Cooking",
                    "Sports",
                    "Travel",
                    "Music",
                    "Art",
                    "Photography",
                    "Gardening",
                    "Fitness",
                    "Other",
                ],
            ),
            ProfileField(
                key="travel_frequency",
                label="Travel Frequency",
                field_type="select",
                options=["Frequently (monthly+)", "Occasionally (few times/year)", "Rarely (once a year)", "Never"],
            ),
            ProfileField(
                key="dining_out_frequency",
                label="Dining Out Frequency",
                field_type="select",
                options=["Daily", "Several times a week", "Weekly", "Monthly", "Rarely", "Never"],
            ),
            ProfileField(
                key="alcohol_consumption",
                label="Alcohol Consumption",
                field_type="select",
                options=["Daily", "Weekly", "Monthly", "Rarely", "Never"],
            ),
        ],
    ),
]


def get_category(key: str) -> ProfileCategory | None:
    """Get a category by key."""
    for cat in CATEGORIES:
        if cat.key == key:
            return cat
    return None


def get_all_field_keys() -> dict[str, str]:
    """Return a mapping of 'category.field_key' to human label."""
    result = {}
    for cat in CATEGORIES:
        for field in cat.fields:
            result[f"{cat.key}.{field.key}"] = field.label
    return result
